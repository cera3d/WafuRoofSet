import bpy
import bmesh
import math
from mathutils import Vector

from bpy.props import StringProperty, FloatProperty, EnumProperty, IntProperty, PointerProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper


UNIT_SCALES = {
    "MM": 0.001,
    "CM": 0.01,
    "M": 1.0,
}

EDGE_TYPE_NONE = 0         # 不要(削除候補)
EDGE_TYPE_ROOF_LINE = 1    # 屋根の線(棟・谷・隅棟など。面の境界に使うが高さの基準にはならない)
EDGE_TYPE_EAVE = 2         # 軒先 = 屋根の形(面の境界) + 高さ0の基準
EDGE_TYPE_RIDGE = 3        # 大棟 = 屋根の形(面の境界) + 高さの基準(この線を基準に、離れるほど下がる)
EDGE_TYPE_HEIGHT_REF = 4   # 高さ基準線(壁芯など) = 面の境界には使わない、屋根生成後にZ=0へ自動で合わせるためだけの印

EDGE_TYPE_LAYER_NAME = "roof_edge_type"

EDGE_TYPE_LABELS = {
    EDGE_TYPE_NONE: "不要(削除候補)",
    EDGE_TYPE_ROOF_LINE: "屋根の線",
    EDGE_TYPE_EAVE: "軒先",
    EDGE_TYPE_RIDGE: "大棟",
    EDGE_TYPE_HEIGHT_REF: "高さ基準線",
}


# ---------------------------------------------------------------------------
# DXF 読み込み→線のみのメッシュを作成
# ---------------------------------------------------------------------------

def _load_dxf_edges(filepath, unit_scale=0.001, arc_segments=16):
    import ezdxf
    import math
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()
    edges = []  # (p1(x,y), p2(x,y), layer)

    def scaled(x, y):
        return (x * unit_scale, y * unit_scale)

    for e in msp:
        dxftype = e.dxftype()
        if dxftype == "LINE":
            p1 = scaled(e.dxf.start.x, e.dxf.start.y)
            p2 = scaled(e.dxf.end.x, e.dxf.end.y)
            edges.append((p1, p2, e.dxf.layer))
        elif dxftype == "LWPOLYLINE":
            pts = [scaled(p[0], p[1]) for p in e.get_points()]
            n = len(pts)
            rng = n if e.closed else n - 1
            for i in range(rng):
                edges.append((pts[i], pts[(i + 1) % n], e.dxf.layer))
        elif dxftype == "POLYLINE":
            pts = [scaled(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            n = len(pts)
            rng = n if e.is_closed else n - 1
            for i in range(rng):
                edges.append((pts[i], pts[(i + 1) % n], e.dxf.layer))
        elif dxftype == "ARC":
            # 円弧を、指定した分割数の直線に細分化して読み込む
            center = e.dxf.center
            radius = e.dxf.radius
            start_angle = math.radians(e.dxf.start_angle)
            end_angle = math.radians(e.dxf.end_angle)
            if end_angle < start_angle:
                end_angle += 2 * math.pi
            pts = []
            for i in range(arc_segments + 1):
                ang = start_angle + (end_angle - start_angle) * i / arc_segments
                pts.append(scaled(center.x + radius * math.cos(ang), center.y + radius * math.sin(ang)))
            for i in range(len(pts) - 1):
                edges.append((pts[i], pts[i + 1], e.dxf.layer))
        elif dxftype == "CIRCLE":
            # 円を、指定した分割数の直線(閉じたループ)に細分化して読み込む
            center = e.dxf.center
            radius = e.dxf.radius
            pts = []
            for i in range(arc_segments + 1):
                ang = 2 * math.pi * i / arc_segments
                pts.append(scaled(center.x + radius * math.cos(ang), center.y + radius * math.sin(ang)))
            for i in range(len(pts) - 1):
                edges.append((pts[i], pts[i + 1], e.dxf.layer))
    return edges


_dxf_edges_cache = {"filepath": None, "unit_scale": None, "arc_segments": None, "edges": None}


def _load_dxf_edges_cached(filepath, unit_scale=0.001, arc_segments=16):
    """_load_dxf_edges の結果をキャッシュする。
    スキャン(レイヤー一覧取得)と実際のインポートで同じファイルを2回パースするのを防ぐ
    (大きなDXF、特に確認申請図のような情報量の多いファイルで効果が出る)。
    """
    global _dxf_edges_cache
    if (_dxf_edges_cache["filepath"] == filepath and _dxf_edges_cache["unit_scale"] == unit_scale
            and _dxf_edges_cache["arc_segments"] == arc_segments):
        return _dxf_edges_cache["edges"]

    edges = _load_dxf_edges(filepath, unit_scale=unit_scale, arc_segments=arc_segments)
    _dxf_edges_cache = {"filepath": filepath, "unit_scale": unit_scale, "arc_segments": arc_segments, "edges": edges}
    return edges


def scan_dxf_layers(filepath, unit_scale=0.001, arc_segments=16):
    """DXFファイルに含まれるレイヤー名と、それぞれの線の本数を調べる。
    戻り値: [(レイヤー名, 本数), ...] 本数の多い順。
    """
    edges = _load_dxf_edges_cached(filepath, unit_scale=unit_scale, arc_segments=arc_segments)
    counts = {}
    for _p1, _p2, layer in edges:
        counts[layer] = counts.get(layer, 0) + 1
    return sorted(counts.items(), key=lambda item: -item[1])


def import_dxf_lines(filepath, unit_scale=0.001, layer_filter=None, layer_names=None, arc_segments=16):
    """DXFの線を、面にしないまま編集可能なメッシュとしてインポートする。
    layer_names が指定されていれば、そのレイヤー名の集合に完全一致する線だけを読み込む
    (複数レイヤーの選択に対応)。無ければ layer_filter(部分一致)を使う(従来動作)。
    """
    edges = _load_dxf_edges_cached(filepath, unit_scale=unit_scale, arc_segments=arc_segments)
    if layer_names is not None:
        layer_names_set = set(layer_names)
        edges = [e for e in edges if e[2] in layer_names_set]
    elif layer_filter:
        edges = [e for e in edges if layer_filter in e[2]]
    if not edges:
        raise ValueError("DXFから線データが読み込めませんでした。")

    mesh = bpy.data.meshes.new("RoofLines")
    bm = bmesh.new()
    type_layer = bm.edges.layers.int.new(EDGE_TYPE_LAYER_NAME)

    vert_cache = {}

    def get_vert(x, y):
        key = (round(x, 5), round(y, 5))
        if key in vert_cache:
            return vert_cache[key]
        v = bm.verts.new((x, y, 0.0))
        vert_cache[key] = v
        return v

    for p1, p2, _layer in edges:
        v1 = get_vert(*p1)
        v2 = get_vert(*p2)
        if v1 is v2:
            continue  # 長さゼロに退化した円弧などは無視する
        try:
            edge = bm.edges.new((v1, v2))
        except ValueError:
            edge = bm.edges.get((v1, v2))
        if edge is not None:
            edge[type_layer] = EDGE_TYPE_NONE

    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new("RoofLines", mesh)
    bpy.context.collection.objects.link(obj)
    return obj, len(edges)


def _set_selected_edge_type(obj, edge_type):
    """編集モードで選択中の辺にタイプを設定する。戻り値: 設定した本数。"""
    bm = bmesh.from_edit_mesh(obj.data)
    type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)
    if type_layer is None:
        type_layer = bm.edges.layers.int.new(EDGE_TYPE_LAYER_NAME)
    count = 0
    for edge in bm.edges:
        if edge.select:
            edge[type_layer] = edge_type
            count += 1
    bmesh.update_edit_mesh(obj.data)
    return count


def _offset_selected_edges(obj, distance):
    """編集モードで選択中の辺を、各辺の2D法線方向に distance(m) だけ平行移動する。
    共有頂点は、接続する選択辺の法線の平均方向に移動する。戻り値: 移動した頂点数。

    辺の頂点順序(v1→v2)はDXFインポート時の並び順に依存し、必ずしも図面全体の
    輪郭の向き(時計回り/反時計回り)と一致しない。単純に+90°回転した法線をそのまま
    使うと、辺によって「外向き」と「内向き」が入り混じってしまい、distanceの符号を
    プラスにしてもマイナスにしても、一部の辺だけ意図と逆方向に動くことがあった。
    そのため、各辺の法線が「その辺が属する輪郭(連結成分)自身の重心」から離れる
    方向(外向き)になるよう、辺ごとに向きを補正してから使う。
    メッシュ全体でひとつの重心を使うと、1つのRoofLinesオブジェクトの中に複数の
    独立した屋根の輪郭(離れた場所にある別々の建物・矢印などの残骸を含む)が
    混在している場合に、遠く離れた他の輪郭に重心が引っ張られて、辺によっては
    向きの判定が逆になってしまう(実際に検証したところ、52辺中18辺で判定が
    食い違っていた)。そのため、輪郭ごと(辺で繋がっている頂点のかたまりごと)に
    重心を別々に計算する。
    """
    import mathutils

    bm = bmesh.from_edit_mesh(obj.data)

    all_verts = list(bm.verts)
    if not all_verts:
        bm.free()
        return 0

    # 連結成分(辺で繋がっている頂点のかたまり)ごとにグループ分けし、
    # 各頂点がどの成分の重心を参照すべきかを事前に計算しておく。
    visited = set()
    vert_to_local_centroid = {}
    for start_v in all_verts:
        if start_v in visited:
            continue
        comp = []
        stack = [start_v]
        visited.add(start_v)
        while stack:
            v = stack.pop()
            comp.append(v)
            for e in v.link_edges:
                other = e.other_vert(v)
                if other not in visited:
                    visited.add(other)
                    stack.append(other)
        local_centroid = sum((v.co for v in comp), mathutils.Vector()) / len(comp)
        for v in comp:
            vert_to_local_centroid[v] = local_centroid

    move = {}  # vert -> Vector(法線の和)
    for edge in bm.edges:
        if not edge.select:
            continue
        v1, v2 = edge.verts
        d = v2.co - v1.co
        n = mathutils.Vector((-d.y, d.x, 0.0))  # 水平面での +90° 回転
        if n.length == 0:
            continue
        n.normalize()
        # 辺の中点から、その辺が属する輪郭自身の重心へ向かうベクトルと法線が
        # 同じ向き(内積が正)なら、法線は重心側(内向き)を向いているので反転し、
        # 常に外向きに揃える。
        mid = (v1.co + v2.co) / 2
        local_centroid = vert_to_local_centroid[v1]
        if n.dot(local_centroid - mid) > 0:
            n = -n
        move[v1] = move.get(v1, mathutils.Vector()) + n
        move[v2] = move.get(v2, mathutils.Vector()) + n

    if not move:
        bm.free()
        return 0

    for v, vec in move.items():
        if vec.length > 0:
            vec.normalize()
        v.co += vec * distance

    bmesh.update_edit_mesh(obj.data)
    bm.free()
    return len(move)


def _count_edge_types(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)
    counts = {EDGE_TYPE_NONE: 0, EDGE_TYPE_ROOF_LINE: 0, EDGE_TYPE_EAVE: 0}
    if type_layer is not None:
        for edge in bm.edges:
            counts[edge[type_layer]] = counts.get(edge[type_layer], 0) + 1
    bm.free()
    return counts


# ---------------------------------------------------------------------------
# 壁芯基準で屋根面を生成
# ---------------------------------------------------------------------------

def _point_to_segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    proj_x, proj_y = ax + t * dx, ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _point_to_line_distance(px, py, ax, ay, bx, by):
    """点から、(ax,ay)-(bx,by)を通る無限直線までの垂直距離を返す(延長線上、端点にクランプしない)。
    軒先が段差・切り欠きで複数のセグメントに分かれていても、それらが同一直線上にあれば、
    セグメントの境目でカクつかず、1枚の平らな面として高さを計算できる。
    """
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0:
        return math.hypot(px - ax, py - ay)
    cross = (px - ax) * dy - (py - ay) * dx
    return abs(cross) / length


def generate_roof_from_lines(lines_obj, slope_rad):
    """RoofLinesオブジェクトから屋根面を生成する。
    「屋根の線」と「軒先」の両方を面の境界(トポロジー)に使う。
    高さの基準(距離×0)は「軒先」として設定された線のみ。
    未分類(不要)の線は無視する(事前に削除することを推奨)。
    """
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    mesh = lines_obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)

    boundary_edges = []  # 面の境界に使う線((x1,y1),(x2,y2)) -- 軒先+大棟+屋根の線
    eave_edges = []       # 軒先(高さの基準、離れるほど高くなる)
    ridge_edges = []      # 大棟(高さの基準、離れるほど低くなる)

    for edge in bm.edges:
        v1, v2 = edge.verts
        p1 = (v1.co.x, v1.co.y)
        p2 = (v2.co.x, v2.co.y)
        etype = edge[type_layer] if type_layer is not None else EDGE_TYPE_NONE
        if etype == EDGE_TYPE_NONE or etype == EDGE_TYPE_HEIGHT_REF:
            continue  # 不要の線、および高さ基準線(面の境界には使わない)は無視
        boundary_edges.append((p1, p2))
        if etype == EDGE_TYPE_EAVE:
            eave_edges.append((p1, p2))
        elif etype == EDGE_TYPE_RIDGE:
            ridge_edges.append((p1, p2))
    bm.free()

    if not eave_edges and not ridge_edges:
        raise ValueError("「軒先」または「大棟」として設定された線が1本もありません。先に線を選択して設定してください。")

    if not boundary_edges:
        raise ValueError("屋根の形を作る線がありません。「軒先」「大棟」または「屋根の線」として設定してください。")

    lines = [LineString([p1, p2]) for p1, p2 in boundary_edges]
    merged = unary_union(lines)
    polys = list(polygonize(merged))
    if not polys:
        raise ValueError("面を検出できませんでした。線がちゃんと閉じているか確認してください。")

    def edge_key(p1, p2):
        a = (round(p1[0], 5), round(p1[1], 5))
        b = (round(p2[0], 5), round(p2[1], 5))
        return frozenset((a, b))

    eave_key_set = {edge_key(p1, p2) for p1, p2 in eave_edges}
    ridge_key_set = {edge_key(p1, p2) for p1, p2 in ridge_edges}

    out_mesh = bpy.data.meshes.new("Roof")
    out_bm = bmesh.new()
    vert_cache = {}
    fallback_used = 0

    def get_vert(x, y, z):
        key = (round(x, 5), round(y, 5))
        if key in vert_cache:
            return vert_cache[key]
        v = out_bm.verts.new((x, y, z))
        vert_cache[key] = v
        return v

    def dist_to_edges(x, y, edges):
        return min(_point_to_line_distance(x, y, ax, ay, bx, by) for (ax, ay), (bx, by) in edges)

    face_count = 0
    for poly in polys:
        coords = list(poly.exterior.coords)[:-1]
        n = len(coords)

        # この面自身の境界のうち、軒先・大棟として設定されている辺だけを収集
        own_eaves = []
        own_ridges = []
        for i in range(n):
            a, b = coords[i], coords[(i + 1) % n]
            if edge_key(a, b) in eave_key_set:
                own_eaves.append((a, b))
            elif edge_key(a, b) in ridge_key_set:
                own_ridges.append((a, b))

        # 大棟が設定されていれば、そちらを優先して基準にする
        # (大棟からの距離が離れるほど低くなる。複数の軒先の高さが違う、非対称な屋根に対応するため)
        if own_ridges:
            verts = []
            for x, y in coords:
                dist = dist_to_edges(x, y, own_ridges)
                z = -dist * math.tan(slope_rad)
                verts.append(get_vert(x, y, z))
        else:
            reference_eaves = own_eaves if own_eaves else eave_edges
            if not own_eaves:
                fallback_used += 1

            verts = []
            for x, y in coords:
                dist = dist_to_edges(x, y, reference_eaves)
                z = dist * math.tan(slope_rad)
                verts.append(get_vert(x, y, z))

        try:
            out_bm.faces.new(verts)
            face_count += 1
        except ValueError:
            pass

    out_bm.normal_update()
    out_bm.to_mesh(out_mesh)
    out_bm.free()

    obj = bpy.data.objects.new("Roof", out_mesh)
    bpy.context.collection.objects.link(obj)
    return obj, face_count, fallback_used


def _apply_height_reference_at_point(roof_obj, ref_x, ref_y):
    """(ref_x, ref_y)の真下から真上に光線を飛ばし、roof_obj(生成済みの屋根)の下面と
    最初にぶつかったZ値を調べて、そこがZ=0になるようにroof_objをZ方向に平行移動する。
    """
    from mathutils.bvhtree import BVHTree

    roof_verts_world = [roof_obj.matrix_world @ v.co for v in roof_obj.data.vertices]
    if not roof_verts_world:
        raise ValueError("屋根メッシュに頂点がありません。")
    z_min = min(v.z for v in roof_verts_world)

    roof_bm = bmesh.new()
    roof_bm.from_mesh(roof_obj.data)
    roof_bm.transform(roof_obj.matrix_world)
    bvh = BVHTree.FromBMesh(roof_bm)
    roof_bm.free()

    origin = Vector((ref_x, ref_y, z_min - 10.0))
    direction = Vector((0.0, 0.0, 1.0))
    hit_loc, hit_normal, hit_index, hit_dist = bvh.ray_cast(origin, direction)

    if hit_loc is None:
        raise ValueError("基準線の位置の真下から真上に光線を飛ばしましたが、屋根の面に当たりませんでした。基準線が屋根の範囲内にあるか確認してください。")

    offset_z = hit_loc.z
    roof_obj.location.z -= offset_z
    return offset_z


def get_height_reference_point(lines_obj):
    """lines_obj(RoofLines)の中から「高さ基準線」として設定された辺を探し、
    その代表点(全部の頂点の平均)のワールドXY座標を返す。無ければNoneを返す。
    """
    mesh = lines_obj.data
    mat = lines_obj.matrix_world
    bm = bmesh.new()
    bm.from_mesh(mesh)
    type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)

    pts = []
    if type_layer is not None:
        for edge in bm.edges:
            if edge[type_layer] == EDGE_TYPE_HEIGHT_REF:
                for v in edge.verts:
                    pts.append(mat @ v.co)
    bm.free()

    if not pts:
        return None

    ref_x = sum(p.x for p in pts) / len(pts)
    ref_y = sum(p.y for p in pts) / len(pts)
    return ref_x, ref_y


def set_height_reference(roof_obj, ref_line_obj):
    """ref_line_obj(編集モードで選択した2頂点の線、または全体)のXY位置を基準に、
    roof_objの高さを合わせる(手動で別オブジェクトを選ぶ従来の使い方)。
    """
    mesh = ref_line_obj.data
    mat = ref_line_obj.matrix_world

    if ref_line_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        selected_verts = [v for v in bm.verts if v.select]
        if len(selected_verts) < 2:
            raise ValueError("編集モードでは、基準にしたい線の頂点を2つ以上選択してください。")
        pts = [mat @ v.co for v in selected_verts]
    else:
        pts = [mat @ v.co for v in mesh.vertices]
        if len(pts) < 2:
            raise ValueError("基準線には2頂点以上必要です。")

    ref_x = sum(p.x for p in pts) / len(pts)
    ref_y = sum(p.y for p in pts) / len(pts)
    return _apply_height_reference_at_point(roof_obj, ref_x, ref_y)


def delete_unneeded_lines(lines_obj):
    """「不要(未分類)」の線を削除し、孤立した頂点も一緒に削除する。戻り値: 削除した線の本数"""
    mesh = lines_obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)

    to_delete = [e for e in bm.edges if (e[type_layer] if type_layer is not None else EDGE_TYPE_NONE) == EDGE_TYPE_NONE]
    count = len(to_delete)
    bmesh.ops.delete(bm, geom=to_delete, context='EDGES')

    # 孤立頂点(どの線にもつながっていない頂点)を削除
    isolated = [v for v in bm.verts if len(v.link_edges) == 0]
    bmesh.ops.delete(bm, geom=isolated, context='VERTS')

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return count


# ---------------------------------------------------------------------------
# 勾配設定(x/10 と 角度を連動)
# ---------------------------------------------------------------------------

_updating_slope = False


def _update_slope_from_ratio(self, context):
    global _updating_slope
    if _updating_slope:
        return
    _updating_slope = True
    self.slope_angle = math.degrees(math.atan(self.slope_ratio / 10.0))
    _updating_slope = False


def _update_slope_from_angle(self, context):
    global _updating_slope
    if _updating_slope:
        return
    _updating_slope = True
    self.slope_ratio = 10.0 * math.tan(math.radians(self.slope_angle))
    _updating_slope = False


class DXFLayerItem(bpy.types.PropertyGroup):
    name: StringProperty(name="レイヤー名")
    use: BoolProperty(name="使う", default=True)
    count: IntProperty(name="本数")


class RoofToolProps(bpy.types.PropertyGroup):
    dxf_filepath: StringProperty(
        name="読み込み対象のDXFファイル",
        description="レイヤー一覧を読み込んだ元のDXFファイルパス",
        default="",
    )
    dxf_unit_mode: EnumProperty(
        name="DXFの単位",
        items=[
            ("MM", "mm (ミリ)", "1単位 = 1mm"),
            ("CM", "cm (センチ)", "1単位 = 1cm"),
            ("M", "m (メートル)", "1単位 = 1m"),
        ],
        default="MM",
    )
    dxf_layers: bpy.props.CollectionProperty(type=DXFLayerItem)
    dxf_layers_index: IntProperty(default=0)
    dxf_arc_segments: IntProperty(
        name="円弧の分割数",
        description="DXFの円弧・円を、いくつの直線に分割して読み込むか(多いほど滑らかだが、線の本数が増える)",
        default=16, min=4, max=128,
    )
    dxf_section_collapsed: BoolProperty(
        name="読み込み済み",
        description="読み込みが完了したら自動でON(セクションが折りたたまれる)",
        default=False,
    )

    slope_ratio: FloatProperty(
        name="勾配 (x/10)",
        description="10寸あたりの立ち上がり。例: 4寸勾配なら 4",
        default=4.0, min=0.0, max=20.0, precision=2, step=1,
        update=_update_slope_from_ratio,
    )
    slope_angle: FloatProperty(
        name="勾配 (角度°)",
        description="水平に対する傾斜角",
        default=math.degrees(math.atan(0.4)), min=0.0, max=89.0,
        update=_update_slope_from_angle,
    )
    roof_thickness: FloatProperty(
        name="屋根の厚み (mm)",
        description="屋根の厚み(ミリメートル単位)",
        default=200.0, min=0.0,
    )
    roof_extrude_direction: EnumProperty(
        name="厚みの方向",
        description="屋根の厚みをつける方向",
        items=[
            ('VERTICAL', '鉛直 (Z軸)', '壁芯の平面寸法を維持して真下に押し出します(軒先が垂直)'),
            ('NORMAL', '直角 (法線)', '屋根面に直角に押し出します(ソリッド化モディファイヤと同じ効果)'),
        ],
        default='VERTICAL',
    )
    offset_distance: FloatProperty(
        name="オフセット距離 (mm)",
        description="選択した辺を平行移動する距離(ミリメートル)。正負で方向が変わります",
        default=50.0,
    )


# ---------------------------------------------------------------------------
# Operator / Panel
# ---------------------------------------------------------------------------

class ROOF_UL_dxf_layers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "use", text="")
        row.label(text=item.name)
        row.label(text=str(item.count))


class ROOF_OT_scan_dxf_layers(bpy.types.Operator, ImportHelper):
    """DXFファイルを選び、中に含まれるレイヤー一覧(と本数)を読み込む(まだメッシュ化はしない)"""
    bl_idname = "roof.scan_dxf_layers"
    bl_label = "DXFを開いてレイヤー一覧を読み込む"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".dxf"
    filter_glob: StringProperty(default="*.dxf", options={'HIDDEN'})

    unit_mode: EnumProperty(
        name="DXFの単位",
        items=[
            ("MM", "mm (ミリ)", "1単位 = 1mm"),
            ("CM", "cm (センチ)", "1単位 = 1cm"),
            ("M", "m (メートル)", "1単位 = 1m"),
        ],
        default="MM",
    )

    def execute(self, context):
        try:
            import ezdxf  # noqa: F401
        except ImportError:
            self.report({'ERROR'}, "ezdxf がインストールされていません。")
            return {'CANCELLED'}

        unit_scale = UNIT_SCALES[self.unit_mode]
        props = context.scene.roof_tool_props
        try:
            layer_counts = scan_dxf_layers(self.filepath, unit_scale=unit_scale, arc_segments=props.dxf_arc_segments)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        props.dxf_filepath = self.filepath
        props.dxf_unit_mode = self.unit_mode
        props.dxf_layers.clear()
        for name, count in layer_counts:
            item = props.dxf_layers.add()
            item.name = name
            item.count = count
            item.use = False

        self.report({'INFO'}, f"{len(layer_counts)}個のレイヤーを見つけました。使うレイヤーにチェックを入れて読み込んでください。")
        return {'FINISHED'}


class ROOF_OT_import_dxf_lines(bpy.types.Operator):
    """チェックを入れたレイヤーの線だけを、編集可能な線メッシュ(RoofLines)としてインポート"""
    bl_idname = "roof.import_dxf_lines"
    bl_label = "選択したレイヤーを読み込む"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.roof_tool_props
        return bool(props.dxf_filepath) and len(props.dxf_layers) > 0

    def execute(self, context):
        try:
            import ezdxf  # noqa: F401
            import shapely  # noqa: F401
        except ImportError:
            self.report({'ERROR'}, "ezdxf / shapely がインストールされていません。")
            return {'CANCELLED'}

        props = context.scene.roof_tool_props
        unit_scale = UNIT_SCALES[props.dxf_unit_mode]
        selected_layers = [item.name for item in props.dxf_layers if item.use]
        if not selected_layers:
            self.report({'WARNING'}, "使うレイヤーが1つも選択されていません。")
            return {'CANCELLED'}

        try:
            obj, num_edges = import_dxf_lines(
                props.dxf_filepath, unit_scale=unit_scale, layer_names=selected_layers,
                arc_segments=props.dxf_arc_segments,
            )
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        context.view_layer.objects.active = obj
        obj.select_set(True)
        props.dxf_section_collapsed = True
        self.report({'INFO'}, f"{num_edges}本の線を読み込みました。軒先などを選択して設定してください。")
        return {'FINISHED'}


class ROOF_OT_mark_edge_type(bpy.types.Operator):
    """選択中の線(辺)に種類を設定する(編集モードで使用)"""
    bl_idname = "roof.mark_edge_type"
    bl_label = "選択中の線に種類を設定"
    bl_options = {'REGISTER', 'UNDO'}

    edge_type: IntProperty(default=EDGE_TYPE_EAVE)

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and context.active_object.mode == 'EDIT'
        )

    def execute(self, context):
        obj = context.active_object
        count = _set_selected_edge_type(obj, self.edge_type)
        label = EDGE_TYPE_LABELS.get(self.edge_type, str(self.edge_type))
        _ensure_color_overlay_on(context)
        self.report({'INFO'}, f"{count}本の線を「{label}」に設定しました")
        return {'FINISHED'}


class ROOF_OT_offset_edges(bpy.types.Operator):
    """選択中の辺を、各辺の法線方向に平行移動する(編集モードで使用)"""
    bl_idname = "roof.offset_edges"
    bl_label = "選択辺をオフセット"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and context.active_object.mode == 'EDIT'
        )

    def execute(self, context):
        obj = context.active_object
        dist = context.scene.roof_tool_props.offset_distance / 1000.0
        count = _offset_selected_edges(obj, dist)
        if count == 0:
            self.report({'WARNING'}, "選択されている辺がありません")
            return {'CANCELLED'}
        self.report({'INFO'}, f"{count}個の頂点をオフセットしました")
        return {'FINISHED'}


class ROOF_OT_show_edge_type_counts(bpy.types.Operator):
    """分類状況を確認"""
    bl_idname = "roof.show_edge_type_counts"
    bl_label = "分類状況を確認"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        counts = _count_edge_types(obj)
        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')
        msg = ", ".join(f"{EDGE_TYPE_LABELS[k]}: {v}" for k, v in counts.items())
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class ROOF_OT_generate_roof(bpy.types.Operator):
    """分類済みの線メッシュから屋根面を生成し、厚みを付ける"""
    bl_idname = "roof.generate_roof"
    bl_label = "屋根面を生成"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        try:
            import shapely  # noqa: F401
        except ImportError:
            self.report({'ERROR'}, "shapely がインストールされていません。")
            return {'CANCELLED'}

        import mathutils

        obj = context.active_object
        if obj.mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')

        props = context.scene.roof_tool_props
        slope_rad = math.radians(props.slope_angle)
        desired_thickness = props.roof_thickness / 1000.0  # mm -> m
        extrude_dir = props.roof_extrude_direction

        try:
            roof_obj, face_count, fallback_used = generate_roof_from_lines(obj, slope_rad)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        # ─── 厚み付け処理(鉛直/直角) ───
        if face_count > 0 and desired_thickness > 0:
            bpy.context.view_layer.objects.active = roof_obj
            bpy.ops.object.mode_set(mode='EDIT')
            me = roof_obj.data
            rbm = bmesh.from_edit_mesh(me)

            all_faces = [f for f in rbm.faces]
            original_verts = list(set(v for f in all_faces for v in f.verts))

            vert_vectors = {}

            if extrude_dir == 'VERTICAL':
                cos_theta = math.cos(slope_rad)
                extrude_h = desired_thickness / cos_theta if abs(cos_theta) > 0.001 else desired_thickness
                for v in original_verts:
                    vert_vectors[v] = mathutils.Vector((0, 0, -extrude_h))
            else:
                for v in original_verts:
                    face_normals = [f.normal for f in v.link_faces]
                    if face_normals:
                        avg_normal = sum(face_normals, mathutils.Vector()).normalized()
                        v_dir = mathutils.Vector((0, 0, 0))
                        for f in v.link_faces:
                            v_dir += f.normal
                        v_dir.normalize()

                        dot = v_dir.dot(avg_normal)
                        dist = desired_thickness / dot if abs(dot) > 0.001 else desired_thickness
                        vert_vectors[v] = v_dir * -dist
                    else:
                        vert_vectors[v] = mathutils.Vector((0, 0, -desired_thickness))

            geom = bmesh.ops.extrude_face_region(rbm, geom=all_faces)
            new_verts = [v for v in geom['geom'] if isinstance(v, bmesh.types.BMVert)]

            for nv in new_verts:
                for ov in original_verts:
                    if (nv.co - ov.co).length < 0.0001:
                        nv.co += vert_vectors[ov]
                        break

            bmesh.update_edit_mesh(me)
            bpy.ops.object.mode_set(mode='OBJECT')
        # ─── ここまで ───

        # 「高さ基準線」が設定されていれば、そこがZ=0になるよう自動で高さを合わせる
        height_ref_applied = False
        ref_point = get_height_reference_point(obj)
        if ref_point is not None:
            try:
                _apply_height_reference_at_point(roof_obj, ref_point[0], ref_point[1])
                height_ref_applied = True
            except Exception as e:
                self.report({'WARNING'}, f"高さ基準線はありましたが、自動調整に失敗しました: {e}")

        context.view_layer.objects.active = roof_obj
        roof_obj.select_set(True)
        _ensure_color_overlay_off(context)
        msg = f"屋根を生成しました(面数: {face_count})"
        if fallback_used:
            msg += f" / 自分の軒先がない面: {fallback_used}件(全体で一番近い軒先を代用)"
        if height_ref_applied:
            msg += " / 高さ基準線に合わせて自動調整しました"
        self.report({'WARNING' if fallback_used else 'INFO'}, msg)
        return {'FINISHED'}


class ROOF_OT_set_height_reference(bpy.types.Operator):
    """基準線(アクティブオブジェクト、編集モードなら選択頂点)のXY位置の真下から
    真上に光線を飛ばし、屋根オブジェクト(もう一方の選択オブジェクト)の下面と最初にぶつかった
    Z値を調べて、そこがZ=0になるよう屋根を平行移動する。
    """
    bl_idname = "roof.set_height_reference"
    bl_label = "選択した線を高さ基準にする"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and len(context.selected_objects) == 2

    def execute(self, context):
        ref_line_obj = context.active_object
        others = [o for o in context.selected_objects if o != ref_line_obj]
        if len(others) != 1:
            self.report({'ERROR'}, "基準にしたい線(アクティブ)と、屋根オブジェクトの、合計2つを選択してください。")
            return {'CANCELLED'}
        roof_obj = others[0]

        try:
            offset_z = set_height_reference(roof_obj, ref_line_obj)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        self.report({'INFO'}, f"「{roof_obj.name}」を Z{-offset_z:+.4f}m 移動し、基準線の位置をZ=0にしました。")
        return {'FINISHED'}


class ROOF_OT_delete_unneeded_lines(bpy.types.Operator):
    """「不要」のままの線(軒先も屋根の線も設定していない線)を一括削除する"""
    bl_idname = "roof.delete_unneeded_lines"
    bl_label = "不要な線を削除"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')

        count = delete_unneeded_lines(obj)

        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"不要な線を{count}本削除しました")
        return {'FINISHED'}


def draw_roof_panel(layout, context):
    """屋根アドオンのパネル内容を描く(タブ統合パネルからも、単独パネルからも呼べる)。"""
    props = context.scene.roof_tool_props

    box = layout.box()
    row = box.row(align=True)
    row.label(text="1. 読み込み", icon='IMPORT')
    row.prop(
        props, "dxf_section_collapsed", text="",
        icon='TRIA_RIGHT' if props.dxf_section_collapsed else 'TRIA_DOWN', emboss=False,
    )
    if not props.dxf_section_collapsed:
        box.prop(props, "dxf_arc_segments")
        box.operator(ROOF_OT_scan_dxf_layers.bl_idname)
        if props.dxf_layers:
            col = box.column(align=True)
            col.scale_y = 0.8
            col.label(text="使うレイヤーにチェック(複数選択可):")
            box.template_list(
                "ROOF_UL_dxf_layers", "", props, "dxf_layers", props, "dxf_layers_index", rows=4,
            )
            box.operator(ROOF_OT_import_dxf_lines.bl_idname)
    else:
        box.label(text="読み込み済み(クリックで再表示)")

    box = layout.box()
    box.label(text="2. 自動判定(任意)", icon='AUTO')
    box.operator(ROOF_OT_auto_detect.bl_idname)
    col = box.column(align=True)
    col.scale_y = 0.8
    col.label(text="必ず確認してください。")

    box = layout.box()
    box.label(text="3. 線を指定(編集モードで選択してから)", icon='EDGESEL')
    col = box.column(align=True)
    op = col.operator(ROOF_OT_mark_edge_type.bl_idname, text="軒先として設定")
    op.edge_type = EDGE_TYPE_EAVE
    op = col.operator(ROOF_OT_mark_edge_type.bl_idname, text="大棟として設定")
    op.edge_type = EDGE_TYPE_RIDGE
    op = col.operator(ROOF_OT_mark_edge_type.bl_idname, text="屋根の線として設定")
    op.edge_type = EDGE_TYPE_ROOF_LINE
    op = col.operator(ROOF_OT_mark_edge_type.bl_idname, text="リセット(不要に戻す)")
    op.edge_type = EDGE_TYPE_NONE
    op = col.operator(ROOF_OT_mark_edge_type.bl_idname, text="軒梁中心線に設定")
    op.edge_type = EDGE_TYPE_HEIGHT_REF

    leg = box.column(align=True)
    leg.scale_y = 0.75
    leg.label(text=" 赤=軒先 / オレンジ=大棟 / 紫=軒梁中心線")
    leg.label(text=" 青=屋根の線 / グレー=不要")
    leg2 = box.column(align=True)
    leg2.scale_y = 0.75
    leg2.label(text="軒梁中心線は高さ基準線として使用")
    leg2.label(text="1本のみ設定してください")

    box = layout.box()
    box.label(text="4. 線のオフセット（不安定）", icon='MOD_OFFSET')
    box.prop(props, "offset_distance")
    box.operator(ROOF_OT_offset_edges.bl_idname, text="選択辺をオフセット")
    col = box.column(align=True)
    col.scale_y = 0.8
    col.label(text="線の作成方法によって内側/外側の")
    col.label(text="判定が安定しないことがあります")
    col.separator()
    col.label(text="瓦を敷く際はケラバ・軒の出を考慮し")
    col.label(text="屋根面(野地板面)を小さくしておくのがおすすめ")

    box = layout.box()
    box.label(text="5. 不要な線を整理", icon='TRASH')
    box.operator(ROOF_OT_delete_unneeded_lines.bl_idname)

    box = layout.box()
    box.label(text="6. 勾配", icon='DRIVER_ROTATIONAL_DIFFERENCE')
    row = box.row(align=True)
    row.prop(props, "slope_ratio", text="(x/10)")
    row.prop(props, "slope_angle", text="(角度°)")

    box = layout.box()
    box.label(text="7. 厚み", icon='MOD_SOLIDIFY')
    box.prop(props, "roof_thickness")
    box.prop(props, "roof_extrude_direction")

    box = layout.box()
    box.label(text="8. 屋根面を生成", icon='MESH_DATA')
    box.operator(ROOF_OT_generate_roof.bl_idname)


# ---------------------------------------------------------------------------
# 自動判定(連結成分 + 面接触数で 軒先/屋根の線 を自動分類)
# ---------------------------------------------------------------------------

def auto_detect_roof(lines_obj):
    """孤立した小さな線の塑(矢印など)を不要として自動判定し、
    残った本体の線を面に分割して、1面にしか接していない線を「軒先」、2面に接する線を
    「屋根の線」として自動分類する。
    戻り値: (eave_count, roof_line_count, discarded_count, face_count)
    """
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    mesh = lines_obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)
    if type_layer is None:
        type_layer = bm.edges.layers.int.new(EDGE_TYPE_LAYER_NAME)

    # 連結成分を探す(幅優先探索)
    visited = set()
    components = []
    for start_edge in bm.edges:
        if start_edge in visited:
            continue
        stack = [start_edge]
        comp = []
        visited.add(start_edge)
        while stack:
            e = stack.pop()
            comp.append(e)
            for v in e.verts:
                for e2 in v.link_edges:
                    if e2 not in visited:
                        visited.add(e2)
                        stack.append(e2)
        components.append(comp)

    if not components:
        bm.free()
        return 0, 0, 0, 0

    largest = max(components, key=len)
    discarded_count = sum(len(c) for c in components) - len(largest)

    # まず全部不要(NONE)にしておく
    for e in bm.edges:
        e[type_layer] = EDGE_TYPE_NONE

    edge_coords = []
    for e in largest:
        v1, v2 = e.verts
        edge_coords.append(((v1.co.x, v1.co.y), (v2.co.x, v2.co.y)))

    lines = [LineString([p1, p2]) for p1, p2 in edge_coords]
    merged = unary_union(lines)
    polys = list(polygonize(merged))

    def edge_key(p1, p2):
        a = (round(p1[0], 4), round(p1[1], 4))
        b = (round(p2[0], 4), round(p2[1], 4))
        return frozenset((a, b))

    face_touch_count = {}
    for poly in polys:
        coords = list(poly.exterior.coords)[:-1]
        n = len(coords)
        for i in range(n):
            k = edge_key(coords[i], coords[(i + 1) % n])
            face_touch_count[k] = face_touch_count.get(k, 0) + 1

    eave_count = roof_line_count = 0
    for e in largest:
        v1, v2 = e.verts
        k = edge_key((v1.co.x, v1.co.y), (v2.co.x, v2.co.y))
        touches = face_touch_count.get(k, 0)
        if touches == 1:
            e[type_layer] = EDGE_TYPE_EAVE
            eave_count += 1
        else:
            e[type_layer] = EDGE_TYPE_ROOF_LINE
            roof_line_count += 1

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    return eave_count, roof_line_count, discarded_count, len(polys)


class ROOF_OT_auto_detect(bpy.types.Operator):
    """孤立した矢印などの線を自動で除外し、残った線を軒先/屋根の線に自動分類する"""
    bl_idname = "roof.auto_detect"
    bl_label = "自動判定"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        try:
            import shapely  # noqa: F401
        except ImportError:
            self.report({'ERROR'}, "shapely がインストールされていません。")
            return {'CANCELLED'}

        obj = context.active_object
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        eave_c, roof_c, discarded_c, face_c = auto_detect_roof(obj)

        bpy.ops.object.mode_set(mode='EDIT')
        _ensure_color_overlay_on(context)
        self.report(
            {'INFO'},
            f"自動判定完了: 軒先{eave_c}本 / 屋根の線{roof_c}本 / "
            f"不要{discarded_c}本 / 面{face_c}つ(必ず確認してください)",
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 色分け表示(ビューポートオーバーレイ)
# ---------------------------------------------------------------------------

import gpu
from gpu_extras.batch import batch_for_shader

EDGE_TYPE_COLORS = {
    EDGE_TYPE_NONE: (0.55, 0.55, 0.55, 1.0),       # グレー = 不要
    EDGE_TYPE_ROOF_LINE: (0.2, 0.4, 1.0, 1.0),     # 青 = 屋根の線
    EDGE_TYPE_EAVE: (1.0, 0.15, 0.15, 1.0),        # 赤 = 軒先
    EDGE_TYPE_RIDGE: (1.0, 0.6, 0.0, 1.0),         # オレンジ = 大棟
    EDGE_TYPE_HEIGHT_REF: (0.8, 0.2, 0.9, 1.0),    # 紫 = 高さ基準線
}

_draw_handler = None


def _draw_edge_type_overlay():
    obj = bpy.context.active_object
    if obj is None or obj.type != 'MESH':
        return

    try:
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
            type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)
            owns_bm = False
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            type_layer = bm.edges.layers.int.get(EDGE_TYPE_LAYER_NAME)
            owns_bm = True
    except Exception:
        return

    if type_layer is None:
        if owns_bm:
            bm.free()
        return

    mat = obj.matrix_world
    coords = []
    colors = []
    for edge in bm.edges:
        color = EDGE_TYPE_COLORS.get(edge[type_layer], (1, 1, 1, 1))
        v1, v2 = edge.verts
        coords.append(mat @ v1.co)
        coords.append(mat @ v2.co)
        colors.append(color)
        colors.append(color)

    if owns_bm:
        bm.free()

    if not coords:
        return

    shader = gpu.shader.from_builtin('POLYLINE_SMOOTH_COLOR')
    batch = batch_for_shader(shader, 'LINES', {"pos": coords, "color": colors})
    shader.bind()
    shader.uniform_float("lineWidth", 1.0)
    region = bpy.context.region
    shader.uniform_float("viewportSize", (region.width, region.height))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _ensure_color_overlay_on(context):
    """分類の色分け表示を自動的にONにする"""
    global _draw_handler
    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_edge_type_overlay, (), 'WINDOW', 'POST_VIEW'
        )
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _ensure_color_overlay_off(context):
    """分類の色分け表示を自動的にOFFにする"""
    global _draw_handler
    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


classes = (
    DXFLayerItem,
    RoofToolProps,
    ROOF_UL_dxf_layers,
    ROOF_OT_scan_dxf_layers,
    ROOF_OT_import_dxf_lines,
    ROOF_OT_auto_detect,
    ROOF_OT_offset_edges,
    ROOF_OT_mark_edge_type,
    ROOF_OT_delete_unneeded_lines,
    ROOF_OT_generate_roof,
    ROOF_OT_set_height_reference,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.roof_tool_props = PointerProperty(type=RoofToolProps)


def unregister():
    global _draw_handler
    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None
    del bpy.types.Scene.roof_tool_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

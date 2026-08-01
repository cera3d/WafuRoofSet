import bpy
import bmesh
import math
import re
import os
from mathutils import Vector, Matrix

from bpy.props import PointerProperty, FloatProperty, StringProperty, BoolProperty


# ---------------------------------------------------------------------------
# コレクションに持たせる寸法スペック(平瓦セットごとの共通寸法)
# ---------------------------------------------------------------------------

class KawaraPatternSpec(bpy.types.PropertyGroup):
    hataraki_haba: FloatProperty(
        name="働き幅 (横ピッチ, mm)", default=300.0, min=1.0, precision=0,
        description="瓦1枚が横方向に占める実際の間隔(mm)",
    )
    hataraki_nagasa: FloatProperty(
        name="働き長さ (段ピッチ, mm)", default=280.0, min=1.0, precision=0,
        description="瓦1枚が勾配方向に占める実際の間隔(mm)",
    )
    mune_pitch: FloatProperty(
        name="棟のピッチ (mm)", default=360.0, min=1.0, precision=0,
        description="棟瓦を並べる間隔(mm)",
    )
    sumi_mune_pitch: FloatProperty(
        name="隅棟のピッチ (mm)", default=360.0, min=1.0, precision=0,
        description="隅棟瓦を並べる間隔(mm)",
    )
    chousei_sunpo: FloatProperty(
        name="調整可能寸法 (± mm)", default=10.0, min=0.0, precision=0,
        description="屋根寸法にきっちり収まるよう働き寸法を微調整できる範囲(mm)",
    )
    eave_overhang: FloatProperty(
        name="軒先の出 (mm)", default=0.0, min=0.0, precision=0,
        description="野地板(屋根面)の軒先ラインから、瓦が勾配方向にどれだけはみ出すか(mm)",
    )
    kerava_overhang: FloatProperty(
        name="ケラバの出 (mm)", default=0.0, min=0.0, precision=0,
        description="野地板(屋根面)のケラバラインから、瓦が横方向にどれだけはみ出すか(mm)",
    )
    chidori: BoolProperty(
        name="千鳥敷き", default=False,
        description="平瓦を1段おきに半ピッチ(働き幅の半分)ずらして千鳥状に敷く。"
                     "ずれてケラバからはみ出た分は、後で「はみ出た平瓦をカット」で切り落とす前提",
    )


# ---------------------------------------------------------------------------
# コレクション内のオブジェクトを役割ごとに検索
# ---------------------------------------------------------------------------
#
# [瓦オブジェクトの原点(=Blenderのオブジェクト原点)についての大前提]
# 原点は「瓦メーカーの図面が寸法を測る基準点(屋根面からの距離)」に合わせる。
# bboxの中心・角ではなく、瓦の物理的な引っ掛かり位置(実際に納まる基準)に置く。
#
# 特に重要なルール(平瓦・左右袖瓦のような、ピッチで繰り返し敷く役割同士):
# 原点からのY方向(勾配方向)のbbox範囲を、お互いに一致させておくこと。
# これがズレていると、瓦自体は正しく並んでいるように見えても、袖瓦だけ
# 数mm~十数mm、他の瓦と勾配方向にズレて見える(実際にあったバグ:
# 万十瓦セットの右袖瓦が15mm分ズレていた原因はこれだった)。
# 軒瓦(eave)は1回しか置かれない(繰り返さない)ので、このY bbox一致ルールは
# 免除される。ただしX方向(横ピッチ)だけは平瓦と合わせる必要がある。
# (詳細はWafuRoofSet_kawaraset_readme.md を参照)

VALID_KAWARA_ROLES = {"flat", "eave", "verge_left", "verge_right", "verge_left_eave", "verge_right_eave", "ridge", "hip", "ridge_tomoe", "ridge_oni", "hip_tomoe", "hip_oni"}


def find_role_objects(col):
    """コレクション内のオブジェクトを役割ごとに検索する。
    各オブジェクトのカスタムプロパティ "kawara_role" が設定されていればそれを優先する
    (言語に依存しない、確実な判定方法)。
    設定されていない場合のみ、従来通り名前(日本語/英語)から判定する
    (「隅棟」は「棟」を含むため、hip の判定を ridge より先に行う)。
    """
    found = {}
    for obj in col.objects:
        role = obj.get("kawara_role")
        if role in VALID_KAWARA_ROLES:
            found.setdefault(role, obj)
            continue

        name_lower = obj.name.lower()
        if "隅棟" in obj.name or "hip" in name_lower:
            found.setdefault("hip", obj)
        elif "右袖軒" in obj.name or "verge_right_eave" in name_lower:
            found.setdefault("verge_right_eave", obj)
        elif "左袖軒" in obj.name or "verge_left_eave" in name_lower:
            found.setdefault("verge_left_eave", obj)
        elif "右袖" in obj.name or "verge_right" in name_lower:
            found.setdefault("verge_right", obj)
        elif "左袖" in obj.name or "verge_left" in name_lower:
            found.setdefault("verge_left", obj)
        elif "軒瓦" in obj.name or "eave" in name_lower:
            found.setdefault("eave", obj)
        elif "巴" in obj.name or "tomoe" in name_lower:
            found.setdefault("ridge_tomoe", obj)
        elif "鬼" in obj.name or "oni" in name_lower:
            found.setdefault("ridge_oni", obj)
        elif "棟" in obj.name or "ridge" in name_lower:
            found.setdefault("ridge", obj)
        elif "平" in obj.name or "flat" in name_lower:
            found.setdefault("flat", obj)
    return found


# ---------------------------------------------------------------------------
# 面のフレーム(原点・横軸・縦軸・法線)を求める
# ---------------------------------------------------------------------------

def _reorder_eave_first(verts, edge_is_boundary):
    """各辺の平均Zが一番低い辺を「軒先(index0→1)」とみなし、そこがリストの先頭に
    来るよう回転させる(頂点の並び順に関わらず、常に一番低い辺からスタートする)。
    """
    n = len(verts)
    if n < 3:
        return verts, edge_is_boundary
    edge_avg_z = [(verts[i].z + verts[(i + 1) % n].z) / 2.0 for i in range(n)]
    min_i = min(range(n), key=lambda i: edge_avg_z[i])
    new_verts = verts[min_i:] + verts[:min_i]
    new_edges = edge_is_boundary[min_i:] + edge_is_boundary[:min_i]
    return new_verts, new_edges


def _extract_face_data(bm_face, mat):
    """bmeshの面(BMFace)から、頂点(ワールド座標)と、各辺が「本当に他の屋根面と
    共有されている境界かどうか」のリストを取り出す。
    共有相手の面が上向き(法線Z成分がプラス)なら本物の隣接屋根面(谷・隅棟など)とみなし、
    横向き・下向き(厚み付けでできた側面・底面)なら共有扱いにせず、実際の軒先・ケラバとして扱う。
    これにより、厚み付け済みのソリッドな屋根オブジェクトでも正しく判定できる。
    """
    face_verts = list(bm_face.verts)
    n = len(face_verts)
    mat_normal = mat.to_3x3()

    edge_is_boundary = []
    for i in range(n):
        v1 = face_verts[i]
        v2 = face_verts[(i + 1) % n]
        shared_edge = None
        for e in v1.link_edges:
            if v2 in e.verts:
                shared_edge = e
                break

        is_boundary = True
        if shared_edge is not None:
            for other_face in shared_edge.link_faces:
                if other_face == bm_face:
                    continue
                other_normal_world = (mat_normal @ other_face.normal).normalized()
                if other_normal_world.z > 0.05:
                    is_boundary = False
                    break

        edge_is_boundary.append(is_boundary)

    verts = [mat @ v.co for v in face_verts]
    return verts, edge_is_boundary


def _get_face_verts(face_obj):
    """対象オブジェクトから、屋根面を構成する頂点(ワールド座標、軒先が先頭に来るよう
    並べ替え済み)、face_index(そのオブジェクト内での面インデックス。オブジェクト全体を
    1面として扱った場合はNone)、各辺が境界(他の面と共有されていない)かどうかのリストを返す。
    編集モードで面を1つ選択している場合はその面だけを使い(複数面が1つのメッシュに
    まとまっているオブジェクトからでも、必要な面だけを取り出せる)、
    それ以外の場合は従来通りメッシュ全体の頂点を1つの面として扱う。
    """
    mat = face_obj.matrix_world

    if face_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(face_obj.data)
        selected_faces = [f for f in bm.faces if f.select]
        if len(selected_faces) != 1:
            raise ValueError(
                f"編集モードでは、対象の面をちょうど1つだけ選択してください"
                f"(現在{len(selected_faces)}個選択されています)。"
            )
        target_face = selected_faces[0]
        verts, edge_is_boundary = _extract_face_data(target_face, mat)
        verts, edge_is_boundary = _reorder_eave_first(verts, edge_is_boundary)
        return verts, target_face.index, edge_is_boundary

    mesh = face_obj.data
    verts = [mat @ v.co for v in mesh.vertices]
    edge_is_boundary = [True] * len(verts)
    verts, edge_is_boundary = _reorder_eave_first(verts, edge_is_boundary)
    return verts, None, edge_is_boundary


def _get_face_verts_by_index(face_obj, face_index):
    """face_index を指定して、そのオブジェクトの特定の面の頂点(ワールド座標、軒先が
    先頭に来るよう並べ替え済み)と、各辺の境界判定を取得する。
    face_index が None ならオブジェクト全体を1面として扱う(従来動作)。
    """
    mat = face_obj.matrix_world
    mesh = face_obj.data

    if face_index is None:
        verts = [mat @ v.co for v in mesh.vertices]
        edge_is_boundary = [True] * len(verts)
        return _reorder_eave_first(verts, edge_is_boundary)

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    target_face = bm.faces[face_index]
    verts, edge_is_boundary = _extract_face_data(target_face, mat)
    bm.free()
    return _reorder_eave_first(verts, edge_is_boundary)


def _face_ref_suffix(face_obj, face_index):
    """面ごとに一意になる名前サフィックスを作る(同じオブジェクト内の複数面が
    同じ名前で上書きされないようにするため)。"""
    if face_index is None:
        return face_obj.name
    return f"{face_obj.name}_f{face_index}"


def _tag_face_reference(obj, face_obj, face_index, role):
    """瓦オブジェクトに「どのオブジェクトのどの面から作られたか」を記録しておく。
    これにより、後でカットする時に対象オブジェクトを選び直さなくても
    自動的に元の面を思い出せる。
    """
    obj["kawara_face_object"] = face_obj.name
    obj["kawara_face_index"] = face_index if face_index is not None else -1
    obj["kawara_role"] = role


def _get_face_verts_for_tile(tile_obj):
    """瓦オブジェクトに記録された参照情報から、元の面の頂点(ワールド座標)を取得する。"""
    face_obj_name = tile_obj.get("kawara_face_object")
    if face_obj_name is None:
        raise ValueError(f"「{tile_obj.name}」には元の面の情報が記録されていません。")
    face_obj = bpy.data.objects.get(face_obj_name)
    if face_obj is None:
        raise ValueError(f"元の屋根面オブジェクト「{face_obj_name}」が見つかりません。")
    face_index = tile_obj.get("kawara_face_index", -1)
    face_index = None if face_index is None or face_index < 0 else face_index
    return _get_face_verts_by_index(face_obj, face_index)


def _get_face_frame(face_obj):
    """対象オブジェクトが3頂点以上の平面(矩形・三角形・台形など)であることを前提に、
    原点・横軸・縦軸(勾配方向)・法線・幅・流れ長さ・局所2D座標(点群フィルタ用)を求める。
    軒側の辺は verts[0]→verts[1] と仮定する(既にZが一番低い辺が先頭に来るよう
    並べ替え済み)。
    """
    verts, face_index, edge_is_boundary = _get_face_verts(face_obj)
    n = len(verts)

    if n < 3:
        raise ValueError("対象オブジェクトは3頂点以上の平面である必要があります。")

    origin = verts[0]
    x_axis = (verts[1] - verts[0]).normalized()

    # Newellの方法で法線を求める(3頂点超の面でも安定)
    normal = Vector((0.0, 0.0, 0.0))
    for i in range(n):
        v_curr = verts[i]
        v_next = verts[(i + 1) % n]
        normal.x += (v_curr.y - v_next.y) * (v_curr.z + v_next.z)
        normal.y += (v_curr.z - v_next.z) * (v_curr.x + v_next.x)
        normal.z += (v_curr.x - v_next.x) * (v_curr.y + v_next.y)
    normal.normalize()

    y_axis = normal.cross(x_axis).normalized()

    local_coords = []
    for v in verts:
        rel = v - origin
        local_coords.append((rel.dot(x_axis), rel.dot(y_axis)))

    us = [c[0] for c in local_coords]
    ws = [c[1] for c in local_coords]
    width = max(us) - min(us)
    slope_len = max(ws) - min(ws)

    return origin, x_axis, y_axis, normal, width, slope_len, local_coords, face_index, edge_is_boundary


def _rotation_euler(x_axis, y_axis, normal):
    rot_mat = Matrix((
        (x_axis.x, y_axis.x, normal.x),
        (x_axis.y, y_axis.y, normal.y),
        (x_axis.z, y_axis.z, normal.z),
    ))
    return rot_mat.to_euler()


# ---------------------------------------------------------------------------
# Geometry Nodes: 点群 + 単一オブジェクトを一定回転でインスタンス
# ---------------------------------------------------------------------------

def _get_or_create_instance_node_group():
    ng = bpy.data.node_groups.get("KawaraTileLay")
    if ng is not None:
        return ng

    ng = bpy.data.node_groups.new("KawaraTileLay", "GeometryNodeTree")
    ng.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    rot_in = ng.interface.new_socket(name="Rotation", in_out='INPUT', socket_type='NodeSocketVector')
    rot_in.subtype = 'EULER'
    seed_in = ng.interface.new_socket(name="Random Seed", in_out='INPUT', socket_type='NodeSocketInt')
    seed_in.default_value = 0
    ng.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    nodes = ng.nodes
    links = ng.links
    n_in = nodes.new("NodeGroupInput")
    n_in.location = (-800, 0)
    n_out = nodes.new("NodeGroupOutput")
    n_out.location = (500, 0)

    # 瓦1枚(点)ごとに固有のランダム値を、実体化される前に属性として焼き込んでおく
    # (Realize Instances した後だと全部同じオブジェクトになってしまい、
    #  シェーダー側の「オブジェクト情報→ランダム」では瓦ごとの差が出せないため)
    # シード値(Random Seed)を足すことで、同じ点の並びでもボタン一つでパターンを振り直せる。
    n_index = nodes.new("GeometryNodeInputIndex")
    n_index.location = (-800, -150)
    n_add_seed = nodes.new("ShaderNodeMath")
    n_add_seed.operation = 'ADD'
    n_add_seed.location = (-650, -150)
    n_random = nodes.new("FunctionNodeRandomValue")
    n_random.data_type = 'FLOAT'
    n_random.location = (-500, -150)
    n_store_attr = nodes.new("GeometryNodeStoreNamedAttribute")
    n_store_attr.data_type = 'FLOAT'
    n_store_attr.domain = 'POINT'
    n_store_attr.inputs["Name"].default_value = "kawara_random"
    n_store_attr.location = (-350, -150)

    n_objinfo = nodes.new("GeometryNodeObjectInfo")
    n_objinfo.location = (-800, -400)
    n_objinfo.inputs["As Instance"].default_value = True
    n_instance = nodes.new("GeometryNodeInstanceOnPoints")
    n_instance.location = (-100, 0)
    n_realize = nodes.new("GeometryNodeRealizeInstances")
    n_realize.location = (250, 0)

    links.new(n_index.outputs["Index"], n_add_seed.inputs[0])
    links.new(n_in.outputs["Random Seed"], n_add_seed.inputs[1])
    links.new(n_add_seed.outputs["Value"], n_random.inputs["Seed"])
    links.new(n_in.outputs["Geometry"], n_store_attr.inputs["Geometry"])
    links.new(n_random.outputs["Value"], n_store_attr.inputs["Value"])
    links.new(n_store_attr.outputs["Geometry"], n_instance.inputs["Points"])
    links.new(n_objinfo.outputs["Geometry"], n_instance.inputs["Instance"])
    links.new(n_in.outputs["Rotation"], n_instance.inputs["Rotation"])
    links.new(n_instance.outputs["Instances"], n_realize.inputs["Geometry"])
    links.new(n_realize.outputs["Geometry"], n_out.inputs["Geometry"])
    return ng


def _set_parent_keep_transform(obj, parent):
    """objの親をparentに設定する。現在のワールド座標が変わらないよう、
    parent_inverse行列も一緒に設定する(親を後で動かすと、子も一緒に追従する)。
    """
    obj.parent = parent
    obj.matrix_parent_inverse = parent.matrix_world.inverted()


def _get_or_create_kawara_collection(reference_obj):
    """reference_obj(屋根面やラインオブジェクト)が入っているコレクションの中に、
    生成した瓦を格納する「瓦」という子コレクションを作る(既にあればそれを使う)。
    """
    parent_collections = reference_obj.users_collection
    parent_col = parent_collections[0] if parent_collections else bpy.context.scene.collection

    for child in parent_col.children:
        if child.name == "瓦":
            return child

    new_col = bpy.data.collections.new("瓦")
    parent_col.children.link(new_col)
    return new_col


def _make_point_cloud(name, points, collection=None):
    old = bpy.data.objects.get(name)
    if old:
        old_mesh = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if old_mesh and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)

    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    for p in points:
        bm.verts.new(p)
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(name, mesh)
    target_collection = collection if collection is not None else bpy.context.collection
    target_collection.objects.link(obj)
    return obj


def _instance_on_points(pts_obj, tile_obj, euler):
    ng = _get_or_create_instance_node_group()

    ng_name = f"KawaraTileLay_{tile_obj.name}"
    obj_ng = bpy.data.node_groups.get(ng_name)
    if obj_ng is None:
        obj_ng = ng.copy()
        obj_ng.name = ng_name

    # 既存のノードグループを再利用する場合でも、対象オブジェクトの参照が
    # (削除・作り直し等で)外れていることがあるので、毎回必ず設定し直す
    for n in obj_ng.nodes:
        if n.bl_idname == "GeometryNodeObjectInfo":
            n.inputs["Object"].default_value = tile_obj

    mod = pts_obj.modifiers.new(name="KawaraTileLay", type='NODES')
    mod.node_group = obj_ng

    rot_id = None
    seed_id = None
    for item in obj_ng.interface.items_tree:
        if item.name == "Rotation" and item.item_type == 'SOCKET':
            rot_id = item.identifier
        elif item.name == "Random Seed" and item.item_type == 'SOCKET':
            seed_id = item.identifier
    if rot_id:
        mod[rot_id] = (euler.x, euler.y, euler.z)
    if seed_id:
        scene = bpy.context.scene
        mod[seed_id] = scene.kawara_tool_props.random_seed if hasattr(scene, "kawara_tool_props") else 0

    pts_obj.data.update()
    return mod


# ---------------------------------------------------------------------------
# 平瓦を屋根面に敷く
# ---------------------------------------------------------------------------

def _fit_pitch(length, base_pitch, tolerance, tile_width=None):
    """長さ(length)に対して base_pitch を基準にできるだけジャストで割り付ける。
    詰める方向/伸ばす方向どちらかが tolerance 以内に収まればそれを採用し、
    どちらも収まらなければ、本来のピッチのまま1枚多く(あふれは呼び出し側でトリミング)。

    tile_width: 瓦自体の実際の幅(m)。指定すると、「最後の1枚は次の瓦と重ならない」
    ことを考慮した計算になる(N枚敷くのに必要な長さ = (N-1)*ピッチ + 瓦自体の幅、
    という前提で、N・ピッチを求める)。指定しない場合は、従来通り
    「長さ ÷ 枚数」の単純な計算のまま(後方互換のため)。

    戻り値: (枚数, 実際のピッチ)
    """
    if tile_width is None:
        n_floor = max(1, math.floor(length / base_pitch))
        n_ceil = max(1, math.ceil(length / base_pitch))

        pitch_ceil_fit = length / n_ceil
        pitch_floor_fit = length / n_floor
    else:
        gap_length = length - tile_width  # (N-1)本のピッチの合計に相当する長さ
        n_floor = max(1, math.floor(gap_length / base_pitch) + 1)
        n_ceil = max(1, math.ceil(gap_length / base_pitch) + 1)

        pitch_ceil_fit = gap_length / (n_ceil - 1) if n_ceil > 1 else base_pitch
        pitch_floor_fit = gap_length / (n_floor - 1) if n_floor > 1 else base_pitch

    adjust_ceil = base_pitch - pitch_ceil_fit
    adjust_floor = pitch_floor_fit - base_pitch

    if adjust_ceil <= tolerance + 1e-9:
        return n_ceil, pitch_ceil_fit
    elif adjust_floor <= tolerance + 1e-9:
        return n_floor, pitch_floor_fit
    else:
        return n_ceil, base_pitch


def lay_flat_tiles(face_obj, col):
    """屋根面(平面ポリゴン)に、平瓦だけを隙間なく・はみ出さず敷く。
    軒瓦・袖瓦・棟瓦はここでは一切扱わない(それぞれ別関数で、線に沿って独立に敷く)。
    """
    from shapely.geometry import Polygon, Point

    spec = bpy.context.scene.kawara_working_spec
    roles = find_role_objects(col)
    flat_obj = roles.get("flat")
    if flat_obj is None:
        raise ValueError("コレクション内に「平」瓦が見つかりません。")

    origin, x_axis, y_axis, normal, width, slope_len, local_coords, face_index, edge_is_boundary = _get_face_frame(face_obj)
    euler = _rotation_euler(x_axis, y_axis, normal)

    base_pitch_x = spec.hataraki_haba / 1000.0  # mm -> m
    pitch_y = spec.hataraki_nagasa / 1000.0  # mm -> m
    tolerance = spec.chousei_sunpo / 1000.0  # mm -> m

    # 袖瓦が「かぶせ」ではなく「敷く」タイプ(万十など)の場合、
    # 袖瓦オブジェクト自身に記録された kawara_flat_offset_mm の分だけ、
    # 平瓦・軒瓦のグリッド基準点を内側にずらす(袖瓦と1枚目が重ならないようにするため)。
    # 属性が無い(かぶせタイプ、または未設定)場合は 0 のまま、従来通り動作する。
    # 基準(u=0)はケラバそのもの。オフセットがあればケラバからオフセット分内側から
    # 敷き始める(ケラバの出があっても、袖瓦がある側はそちらが実際の出際を
    # 担っているので、この基準点自体はケラバの出の影響を受けない)。
    verge_left_obj = roles.get("verge_left")
    verge_right_obj = roles.get("verge_right")
    # 袖瓦オブジェクトがセットにあっても、この面のその辺自体が境界(ケラバ)でなければ
    # (=隅棟などで他の面と繋がっていて袖瓦が敷かれない辺なら)、オフセットは適用しない。
    n_verts = len(local_coords)
    left_is_boundary = edge_is_boundary[(n_verts - 1) % n_verts]
    right_is_boundary = edge_is_boundary[1 % n_verts]

    # 隅棟など境界でない辺が、軒先から棟にかけて外側(面の外側)へ広がっている場合、
    # ケラバのオーバーハングと同じ考え方で、その辺だけ瓦1枚分(ベースのピッチ分)
    # 外側に張り出した「仮想の面」を一時的に想定して、以降の配置計算(幅・ポリゴン・
    # グリッド範囲すべて)をその仮想の面基準で行う。実際のメッシュは一切変更せず、
    # ここで作るのはあくまで配置計算専用のローカル座標のコピー。
    # 判定は、その辺の傾き(du/dw)だけで行うので、頂点の並び順や左右がどちらを
    # 向いていても正しく働く。
    local_coords = list(local_coords)
    widening_left = False
    widening_right = False
    if not left_is_boundary:
        idx_last = (n_verts - 1) % n_verts
        u_top, w_top = local_coords[idx_last]
        if w_top > 1e-6 and u_top < -1e-6:
            widening_left = True
            local_coords[idx_last] = (u_top - base_pitch_x, w_top)
    if not right_is_boundary:
        idx1 = 1 % n_verts
        idx2 = 2 % n_verts
        u1, w1 = local_coords[idx1]
        u2, w2 = local_coords[idx2]
        if (w2 - w1) > 1e-6 and (u2 - u1) > 1e-6:
            widening_right = True
            local_coords[idx1] = (u1 + base_pitch_x, w1)
            local_coords[idx2] = (u2 + base_pitch_x, w2)
    if widening_left or widening_right:
        us_tmp = [c[0] for c in local_coords]
        width = max(us_tmp) - min(us_tmp)

    left_offset = (verge_left_obj.get("kawara_flat_offset_mm", 0.0) / 1000.0) if (verge_left_obj is not None and left_is_boundary) else 0.0
    right_offset = (verge_right_obj.get("kawara_flat_offset_mm", 0.0) / 1000.0) if (verge_right_obj is not None and right_is_boundary) else 0.0

    # ケラバの出(kerava_overhang)・軒の出(eave_overhang): 割付計算(何枚敷けるか)
    # では、屋根面の幅に、ケラバの出を左右2か所分足した「仮想屋根面」の幅を使う。
    # 最後の1枚は次の瓦にかぶせてもらえない分、瓦自体の実寸(tile_width)がそのまま
    # 必要になる(_fit_pitchのtile_width引数が担う)。基準点(1枚目の位置)は
    # 袖瓦オフセットのみで決まり、ケラバの出はここでは差し引かない。
    kerava_overhang = spec.kerava_overhang / 1000.0
    eave_overhang = spec.eave_overhang / 1000.0

    effective_width = max(width + 2 * kerava_overhang - left_offset, 1e-6)
    tile_width_mm = flat_obj.get("kawara_tile_width_mm")
    tile_width = (tile_width_mm / 1000.0) if tile_width_mm is not None else None
    n_x, pitch_x = _fit_pitch(effective_width, base_pitch_x, tolerance, tile_width=tile_width)

    us = [c[0] for c in local_coords]
    ws = [c[1] for c in local_coords]
    u_min, u_max = min(us), max(us)
    w_min, w_max = min(ws), max(ws)

    poly = Polygon(local_coords)
    if not poly.is_valid:
        poly = poly.buffer(0)
    # 仮想屋根面: 割付判定用のバッファは、ピッチの半分と、ケラバの出・軒の出の
    # うち大きい方を比べて、より大きい方を使う(狭い場所での取りこぼし防止に
    # 加えて、ケラバ・軒の出の分まで判定範囲を広げるため)。
    margin = max(min(pitch_x, pitch_y) * 0.5, kerava_overhang, eave_overhang)
    poly_check = poly.buffer(margin)
    # 隅棟などケラバでない辺(面がその方向に広がったり狭まったりする辺)専用の、
    # より大きい(1ピッチ分)判定バッファ。急な隅棟の角度だと通常のバッファ(半ピッチ)
    # だけでは境界ぎりぎりの1枚を取りこぼすことがあるため。ただしこのバッファは、
    # 「ケラバでない辺の外側にはみ出した候補」にだけ適用し(下のループ内で使用)、
    # 軒先・棟・ケラバ側の判定には一切影響させない(軒瓦の行の判定などに
    # 副作用が出ないようにするため)。
    poly_check_hip = poly.buffer(pitch_x) if not (left_is_boundary and right_is_boundary) else None

    chidori = bool(getattr(spec, "chidori", False))
    # 千鳥敷きの場合、ずらした行は境界の外側(半ピッチ分)まで格子点が要るため、
    # 探索範囲を1列分余分に広げておく。
    i_margin = 2 if chidori else 1
    i_start = math.floor((u_min - kerava_overhang) / pitch_x) - i_margin
    i_end = math.ceil((u_max + kerava_overhang) / pitch_x) + i_margin
    j_start = math.floor((w_min - eave_overhang) / pitch_y) - 1
    j_end = math.ceil(w_max / pitch_y) + 1

    roles = find_role_objects(col)
    has_eave = roles.get("eave") is not None and edge_is_boundary[0]

    right_bound = width - right_offset

    valid_j = []
    candidates = []
    for j in range(j_start, j_end + 1):
        # 千鳥敷き: 偶数段だけ半ピッチ分マイナス側(ケラバの外側)にずらす。
        # はみ出た分は境界チェックでは弾かず、後の「はみ出た平瓦をカット」で
        # ケラバの実際の輪郭に合わせて切り落とす前提。
        # 軒に一番近い行(j=0相当)は基準通り、その次の行から半ピッチずつ交互にずらす
        row_shift = (pitch_x / 2.0) if (chidori and j % 2 == 1) else 0.0
        row_has_point = False
        for i in range(i_start, i_end + 1):
            u = left_offset - row_shift + i * pitch_x
            w = j * pitch_y
            if not chidori:
                # 袖瓦(敷くタイプ)が確保している範囲(左端・右端のオフセット分)には、
                # 平瓦の格子点を置かない
                # 左右の上限・下限は、その辺が実際にケラバ(境界)である場合にのみ適用する。
                # 隅棟などケラバでない辺は、面の形自体が行ごとに広がったり狭まったり
                # するため、固定のleft_offset/right_boundで切ってしまうと、その広がりに
                # 平瓦が追従できなくなる(poly_checkによる面形状の判定だけに任せる)。
                if left_is_boundary and u < left_offset - 1e-6:
                    continue
                if right_is_boundary and u > right_bound + 1e-6:
                    continue
                # _fit_pitch が計算した枚数(n_x)を、境界チェックとは別に、明示的な上限として使う。
                # (ピッチが詰まる方向に調整された場合、境界チェックだけだと、理論値より
                # 1枚多く入ってしまうことがあるため)
                # _fit_pitch が計算した枚数(n_x)による上限も、同じ理由でケラバ側のみに適用する。
                if left_is_boundary and i < 0:
                    continue
                if right_is_boundary and i > n_x - 1:
                    continue
            else:
                # 千鳥のずれた行は、境界を半ピッチ分はみ出すところまで許容する
                # (n_xによる上限チェックも行わない。はみ出しはカット工程に任せる)。
                if left_is_boundary and u < left_offset - pitch_x - 1e-6:
                    continue
                if right_is_boundary and u > right_bound + pitch_x + 1e-6:
                    continue
            point = Point(u, w)
            accept = poly_check.contains(point)
            if not accept and poly_check_hip is not None and not chidori:
                # ケラバでない辺の外側(通常の判定だと弾かれる範囲)に限って、
                # より広いバッファ(1ピッチ分)で再判定する。
                beyond_left = (not left_is_boundary) and u < left_offset - 1e-6
                beyond_right = (not right_is_boundary) and u > right_bound + 1e-6
                if (beyond_left or beyond_right) and poly_check_hip.contains(point):
                    accept = True
            if accept:
                candidates.append((i, j, u, w))
                row_has_point = True
        if row_has_point:
            valid_j.append(j)

    if not candidates:
        raise ValueError("面の範囲内にタイルを1枚も配置できませんでした。面の寸法とピッチを確認してください。")

    j_min_used = min(valid_j)
    flat_pts = [
        origin + x_axis * u + y_axis * w
        for i, j, u, w in candidates
        # 軒瓦がある場合に平瓦を避けるべき行は、常に軒先そのもの(w=0、つまりj==0)を
        # 必ず含めて除外する。隅棟側で候補探索のバッファを広げた影響で、本来は存在
        # しない軒の外側の行(j<0)が候補に紛れ込み、それがj_min_usedになってしまうと、
        # 本来除外すべき本物の軒先の行(j==0)がそのまま残ってしまい、軒瓦と平瓦が
        # 二重に敷かれてしまうため。
        if not (has_eave and (j == 0 or j == j_min_used))
    ]

    if not flat_pts:
        raise ValueError("面の範囲内にタイルを1枚も配置できませんでした。面の寸法とピッチを確認してください。")

    name_suffix = _face_ref_suffix(face_obj, face_index)
    kawara_col = _get_or_create_kawara_collection(face_obj)
    pts_obj = _make_point_cloud(f"{name_suffix}_kawara_flat", flat_pts, collection=kawara_col)
    _instance_on_points(pts_obj, flat_obj, euler)
    _tag_face_reference(pts_obj, face_obj, face_index, "flat")
    pts_obj["kawara_pitch_x_mm"] = pitch_x * 1000.0
    pts_obj["kawara_pitch_y_mm"] = pitch_y * 1000.0
    _set_parent_keep_transform(pts_obj, face_obj)

    return [pts_obj], len(flat_pts)


# ---------------------------------------------------------------------------
# 折れ線に沿ってタイルを配置する共通処理
# ---------------------------------------------------------------------------

def _lay_tiles_along_coords(coords, tile_obj, pitch, name_prefix, collection=None):
    """折れ線(ワールド座標のリスト)に沿って、始点を基準にピッチで敷いていく。
    終点側は多少はみ出すことを許容し(後で終端カットする前提)、
    それとは別に、終点にも常に逆向き(180度回転)の「終端部棟瓦」を1枚追加する。
    曲がり角がある場合は、向きが変わるごとに別の点群オブジェクトに分けて正しい回転を持たせる。
    生成した「順方向」の点群オブジェクトには、終端カット用の座標・向きをタグ付けしておく。
    """
    if len(coords) < 2:
        raise ValueError("ラインには2点以上必要です。")

    segments = []
    total_length = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        seg_vec = p2 - p1
        seg_len = seg_vec.length
        if seg_len <= 1e-9:
            continue
        segments.append((p1, seg_vec.normalized(), seg_len, total_length))
        total_length += seg_len

    if total_length <= 0:
        raise ValueError("ラインの長さが0です。")

    def _segment_at(dist):
        seg = segments[-1]
        for s in segments:
            p1, direction, seg_len, start_dist = s
            if start_dist - 1e-6 <= dist <= start_dist + seg_len + 1e-6:
                seg = s
                break
        return seg

    # はみ出しを許容するため、切り上げ(ceil)で必要な枚数を決める(手前で止めない)
    n_full = max(1, math.ceil((total_length - 1e-6) / pitch))
    forward_distances = [i * pitch for i in range(n_full)]

    # 割り切れる/割り切れないに関わらず、終点にも常に逆向き(180度回転)の「終端部棟瓦」を1枚追加する。
    closing_distance = total_length

    z_axis = Vector((0, 0, 1))

    def _group_by_direction(distances, flip):
        groups = {}
        for dist in distances:
            p1, direction, seg_len, start_dist = _segment_at(dist)
            pos = p1 + direction * (dist - start_dist)
            key = (round(direction.x, 4), round(direction.y, 4), round(direction.z, 4))
            groups.setdefault(key, []).append(pos)

        result = []
        for key, pts in groups.items():
            direction = Vector(key)
            if flip:
                direction = -direction
            x_axis = direction
            y_axis = z_axis.cross(x_axis).normalized()
            normal = x_axis.cross(y_axis).normalized()
            euler = _rotation_euler(x_axis, y_axis, normal)
            result.append((pts, euler, direction))
        return result

    forward_groups = _group_by_direction(forward_distances, flip=False)
    closing_groups = _group_by_direction([closing_distance], flip=True) if closing_distance is not None else []

    end_point = coords[-1]
    end_direction = (coords[-1] - coords[-2]).normalized()

    created = []
    total_count = 0
    idx = 0
    for pts, euler, direction in forward_groups:
        pts_obj = _make_point_cloud(f"{name_prefix}_{idx}", pts, collection=collection)
        _instance_on_points(pts_obj, tile_obj, euler)
        # 終端カット用に、実際に使った終点座標と向きを記録しておく
        pts_obj["kawara_ridge_end_point"] = tuple(end_point)
        pts_obj["kawara_ridge_end_direction"] = tuple(end_direction)
        created.append(pts_obj)
        total_count += len(pts)
        idx += 1
    for pts, euler, direction in closing_groups:
        pts_obj = _make_point_cloud(f"{name_prefix}_{idx}", pts, collection=collection)
        _instance_on_points(pts_obj, tile_obj, euler)
        created.append(pts_obj)
        total_count += len(pts)
        idx += 1

    return created, total_count


def _lay_tiles_along_coords_simple(coords, tile_obj, pitch, name_prefix, collection=None):
    """折れ線に沿って始点から単純にピッチで敷く(隅棟用)。
    終点側は多少はみ出すことを許容し、後で棟のラインでカットする前提。
    終端部棟瓦(180度回転タイル)は追加しない。
    """
    if len(coords) < 2:
        raise ValueError("ラインには2点以上必要です。")

    segments = []
    total_length = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        seg_vec = p2 - p1
        seg_len = seg_vec.length
        if seg_len <= 1e-9:
            continue
        segments.append((p1, seg_vec.normalized(), seg_len, total_length))
        total_length += seg_len

    if total_length <= 0:
        raise ValueError("ラインの長さが0です。")

    z_axis = Vector((0, 0, 1))
    count = max(1, math.floor(total_length / pitch) + 1)

    points_by_direction = {}
    for i in range(count):
        dist = min(i * pitch, total_length)
        seg = segments[-1]
        for s in segments:
            p1, direction, seg_len, start_dist = s
            if start_dist - 1e-6 <= dist <= start_dist + seg_len + 1e-6:
                seg = s
                break
        p1, direction, seg_len, start_dist = seg
        pos = p1 + direction * (dist - start_dist)
        key = (round(direction.x, 4), round(direction.y, 4), round(direction.z, 4))
        points_by_direction.setdefault(key, []).append(pos)

    created = []
    total_count = 0
    for idx, (key, pts) in enumerate(points_by_direction.items()):
        direction = Vector(key)
        x_axis = direction
        y_axis = z_axis.cross(x_axis).normalized()
        normal = x_axis.cross(y_axis).normalized()
        euler = _rotation_euler(x_axis, y_axis, normal)
        pts_obj = _make_point_cloud(f"{name_prefix}_{idx}", pts, collection=collection)
        _instance_on_points(pts_obj, tile_obj, euler)
        created.append(pts_obj)
        total_count += len(pts)

    return created, total_count


def _lay_tiles_along_line_fixed_rotation(coords, tile_obj, pitch, name_prefix, euler, collection=None):
    """coords(折れ線)に沿って等間隔にタイルを配置するが、
    回転はラインの向きではなく、指定された euler(通常は屋根面の傾き)を使う。
    軒瓦・袖瓦のように、屋根面に乗った状態で線に沿って並べたい場合に使う。
    """
    if len(coords) < 2:
        raise ValueError("ラインには2点以上必要です。")

    segments = []
    total_length = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        seg_vec = p2 - p1
        seg_len = seg_vec.length
        if seg_len <= 1e-9:
            continue
        segments.append((p1, seg_vec.normalized(), seg_len, total_length))
        total_length += seg_len

    if total_length <= 0:
        raise ValueError("ラインの長さが0です。")

    count = max(1, math.floor(total_length / pitch) + 1)
    pts = []
    for i in range(count):
        dist = min(i * pitch, total_length)
        seg = segments[-1]
        for s in segments:
            p1, direction, seg_len, start_dist = s
            if start_dist - 1e-6 <= dist <= start_dist + seg_len + 1e-6:
                seg = s
                break
        p1, direction, seg_len, start_dist = seg
        pts.append(p1 + direction * (dist - start_dist))

    pts_obj = _make_point_cloud(f"{name_prefix}_0", pts, collection=collection)
    _instance_on_points(pts_obj, tile_obj, euler)
    return [pts_obj], count


def _lay_verge_tiles_with_dynamic_rotation(coords, tile_obj, pitch, name_prefix, face_normal, collection=None, first_tile_obj=None):
    """
    coords(ケラバの線)に沿って等間隔に配置し、
    回転は「ケラバの進む向き」と「屋根面の法線」から動的に3次元計算する。

    first_tile_obj: 指定した場合、軒に最も近い1段目だけこのオブジェクトを使い、
    2段目以降は tile_obj を使う(「左袖軒」・「右袖軒」のように、1段目専用の
    瓦がセットにある場合用)。
    """
    if len(coords) < 2:
        raise ValueError("ラインには2点以上必要です。")

    segments = []
    total_length = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        seg_vec = p2 - p1
        seg_len = seg_vec.length
        if seg_len <= 1e-9:
            continue
        segments.append((p1, seg_vec.normalized(), seg_len, total_length))
        total_length += seg_len

    if total_length <= 0:
        raise ValueError("ラインの長さが0です。")

    count = max(1, math.floor(total_length / pitch) + 1)
    pts = []

    # 1. ケラバの線が「どの向きに進んでいるか」を取得(1つの直線と仮定)
    _, verge_dir, _, _ = segments[0]

    # 2. 屋根の法線とケラバの向きから、袖瓦用の新しい3軸(ローカル座標系)を計算
    # 袖瓦の「縦方向(Y軸)」をケラバの進行方向に一致させる
    y_axis = verge_dir.normalized()
    normal = face_normal.normalized()
    x_axis = y_axis.cross(normal).normalized()
    y_axis = normal.cross(x_axis).normalized()

    # 3. 正しいオイラー角に変換
    euler = _rotation_euler(x_axis, y_axis, normal)

    for i in range(count):
        dist = min(i * pitch, total_length)
        seg = segments[-1]
        for s in segments:
            p1, direction, seg_len, start_dist = s
            if start_dist - 1e-6 < dist <= start_dist + seg_len + 1e-6:
                seg = s
                break
        p1, direction, seg_len, start_dist = seg
        pts.append(p1 + direction * (dist - start_dist))

    created = []
    if first_tile_obj is not None and pts:
        first_pts, rest_pts = pts[:1], pts[1:]
        first_obj = _make_point_cloud(f"{name_prefix}_eave_0", first_pts, collection=collection)
        _instance_on_points(first_obj, first_tile_obj, euler)
        created.append(first_obj)
        if rest_pts:
            rest_obj = _make_point_cloud(f"{name_prefix}_0", rest_pts, collection=collection)
            _instance_on_points(rest_obj, tile_obj, euler)
            created.append(rest_obj)
    else:
        pts_obj = _make_point_cloud(f"{name_prefix}_0", pts, collection=collection)
        _instance_on_points(pts_obj, tile_obj, euler)
        created.append(pts_obj)

    return created, count


def lay_eave_tiles(face_obj, col, offset_x=None):
    """屋根面の軒側の辺(verts[0]→verts[1])に沿って軒瓦を敷く。
    回転は屋根面の傾きに合わせる(ラインの向きだけでは勾配が反映されないため)。
    offset_x: 平瓦と揃えるための横方向オフセット(m)。Noneの場合、袖瓦の
    kawara_flat_offset_mm属性から自動計算する(平瓦と同じ考え方)。
    """
    roles = find_role_objects(col)
    eave_obj = roles.get("eave")
    if eave_obj is None:
        return [], 0

    origin, x_axis, y_axis, normal, width, slope_len, local_coords, face_index, edge_is_boundary = _get_face_frame(face_obj)

    if not edge_is_boundary[0]:
        return [], 0

    verts, _, _edge_bnd = _get_face_verts(face_obj)

    spec = bpy.context.scene.kawara_working_spec

    # 袖瓦(敷くタイプ)の場合、平瓦と同じオフセットを軒瓦にも適用する。
    # 基準(u=0)はケラバそのもの、オフセットがあればそこから内側から敷き始める
    # (平瓦と同じ考え方。ケラバの出自体は割付計算にのみ使うもので、ここでは
    # 軒瓦のライン上には敷く枚数・位置に影響しない)。
    verge_left_obj = roles.get("verge_left")
    verge_right_obj = roles.get("verge_right")
    # 平瓦と同様、この面のその辺自体が境界(ケラバ)でなければオフセットは適用しない。
    n_verts = len(local_coords)
    left_is_boundary = edge_is_boundary[(n_verts - 1) % n_verts]
    right_is_boundary = edge_is_boundary[1 % n_verts]
    left_offset = (verge_left_obj.get("kawara_flat_offset_mm", 0.0) / 1000.0) if (verge_left_obj is not None and left_is_boundary) else 0.0
    right_offset = (verge_right_obj.get("kawara_flat_offset_mm", 0.0) / 1000.0) if (verge_right_obj is not None and right_is_boundary) else 0.0
    if offset_x is None:
        offset_x = left_offset

    # 平瓦と同じ考え方: 隅棟など境界でない辺が軒先から棟にかけて外側へ広がって
    # いる場合、その辺が軒先と交わる側の端点を、瓦1枚分(ベースのピッチ分)だけ
    # 外側に仮想的に伸ばしてから軒瓦のラインを敷く(実際のメッシュ・頂点は
    # 一切変更しない)。平瓦側は軒に一番近い段を軒瓦に譲る仕組みになっているため、
    # 軒瓦がここまで追従しないと、隅棟の軒側だけ隙間が残ってしまう。
    base_pitch_eave = spec.hataraki_haba / 1000.0
    extend_left = 0.0
    extend_right = 0.0
    if not left_is_boundary:
        idx_last = (n_verts - 1) % n_verts
        u_top, w_top = local_coords[idx_last]
        if w_top > 1e-6 and u_top < -1e-6:
            extend_left = base_pitch_eave
    if not right_is_boundary:
        idx1 = 1 % n_verts
        idx2 = 2 % n_verts
        u1, w1 = local_coords[idx1]
        u2, w2 = local_coords[idx2]
        if (w2 - w1) > 1e-6 and (u2 - u1) > 1e-6:
            extend_right = base_pitch_eave

    coords = [
        verts[0] - x_axis * extend_left + x_axis * offset_x,
        verts[1] + x_axis * extend_right - x_axis * right_offset,
    ]
    euler = _rotation_euler(x_axis, y_axis, normal)

    # ケラバの出(kerava_overhang): 平瓦と同じ考え方で、割付計算(何枚敷けるか)に
    # だけ使う。屋根面の幅に、ケラバの出を左右2か所分足した「仮想屋根面」の
    # 幅を使って枚数・ピッチを決める(基準点自体はオフセットのみで決まる)。
    kerava_overhang = spec.kerava_overhang / 1000.0
    edge_width = max((verts[1] - verts[0]).length + extend_left + extend_right + 2 * kerava_overhang - left_offset, 1e-6)
    tile_width_mm = eave_obj.get("kawara_tile_width_mm")
    tile_width = (tile_width_mm / 1000.0) if tile_width_mm is not None else None
    _, pitch = _fit_pitch(edge_width, spec.hataraki_haba / 1000.0, spec.chousei_sunpo / 1000.0, tile_width=tile_width)

    name_suffix = _face_ref_suffix(face_obj, face_index)
    kawara_col = _get_or_create_kawara_collection(face_obj)
    created, count = _lay_tiles_along_line_fixed_rotation(coords, eave_obj, pitch, f"{name_suffix}_kawara_eave", euler, collection=kawara_col)
    for o in created:
        _tag_face_reference(o, face_obj, face_index, "eave")
        o["kawara_pitch_mm"] = pitch * 1000.0
        _set_parent_keep_transform(o, face_obj)
    return created, count


def lay_verge_tiles(face_obj, col, side):
    """屋根面の左右の辺に沿って袖瓦を敷く。side は 'left' か 'right'。
    軒側の辺(verts[0]→verts[1])を基準に、
    左袖 = verts[-1]→verts[0]、右袖 = verts[1]→verts[2] の辺を使う。
    """
    roles = find_role_objects(col)
    role_key = "verge_left" if side == "left" else "verge_right"
    eave_role_key = "verge_left_eave" if side == "left" else "verge_right_eave"
    verge_obj = roles.get(role_key)
    if verge_obj is None:
        return [], 0
    # セット内に「左袖軒」・「右袖軒」があれば、軒に最も近い1段目だけそちらを使う
    first_tile_obj = roles.get(eave_role_key)

    verts, _, edge_is_boundary = _get_face_verts(face_obj)
    n = len(verts)
    if n < 3:
        raise ValueError("対象オブジェクトは3頂点以上の平面である必要があります。")

    own_edge_index = (n - 1) if side == "left" else 1
    if not edge_is_boundary[own_edge_index % n]:
        return [], 0

    if side == "left":
        coords = [verts[0], verts[-1]]
    else:
        coords = [verts[1], verts[2 % n]]

    origin, x_axis, y_axis, normal, width, slope_len, local_coords, face_index, _unused_edge_bnd = _get_face_frame(face_obj)

    # 袖瓦には専用のピッチ・調整可能寸法は無く、平瓦・軒瓦の長さ方向ピッチ
    # (hataraki_nagasa)にそのまま揃える(_fit_pitchによる調整は行わない)。
    # スタート位置(1枚目、左右袖軒があればそちらが使われる位置)は軒の実際の
    # 端(coords[0])のまま動かさない。左右袖軒がある場合、2枚目(通常の左右袖の
    # 1段目)はピッチ分進んだ位置になり、これが平瓦の軒瓦がある場合の1段目の
    # 位置と自然に一致する。
    spec = bpy.context.scene.kawara_working_spec
    pitch = spec.hataraki_nagasa / 1000.0

    name_suffix = _face_ref_suffix(face_obj, face_index)
    role = "verge_left" if side == "left" else "verge_right"
    name_prefix = f"{name_suffix}_kawara_verge_{'L' if side == 'left' else 'R'}"
    kawara_col = _get_or_create_kawara_collection(face_obj)
    created, count = _lay_verge_tiles_with_dynamic_rotation(coords, verge_obj, pitch, name_prefix, normal, collection=kawara_col, first_tile_obj=first_tile_obj)
    for o in created:
        _tag_face_reference(o, face_obj, face_index, role)
        o["kawara_pitch_mm"] = pitch * 1000.0
        _set_parent_keep_transform(o, face_obj)
    return created, count


# ---------------------------------------------------------------------------
# 棟・隅棟のライン処理
# ---------------------------------------------------------------------------

def _get_ordered_polyline(line_obj):
    """ライン用オブジェクトから、順序付きのワールド座標リストを作る。
    編集モードで一部の辺だけ選択している場合は、その選択された辺だけを使う
    (ループ状のオブジェクト全体を渡しても、選択した区間だけが使われる)。
    折れ線(3頂点以上)にも対応する。
    """
    mat = line_obj.matrix_world

    if line_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(line_obj.data)
        selected_edges = [e for e in bm.edges if e.select]
        owns_bm = False
    else:
        bm = bmesh.new()
        bm.from_mesh(line_obj.data)
        selected_edges = list(bm.edges)
        owns_bm = True

    if not selected_edges:
        if owns_bm:
            bm.free()
        raise ValueError("使用する辺がありません(編集モードなら対象の辺を選択してください)。")

    vert_to_edges = {}
    for e in selected_edges:
        for v in e.verts:
            vert_to_edges.setdefault(v.index, []).append(e)

    verts_by_index = {}
    for e in selected_edges:
        for v in e.verts:
            verts_by_index[v.index] = v

    endpoints = [v_idx for v_idx, edges in vert_to_edges.items() if len(edges) == 1]
    if not endpoints:
        if owns_bm:
            bm.free()
        raise ValueError(
            "選択されている辺が閉じたループになっています。"
            "棟・隅棟には、始点から終点までの開いた1本のラインを選択してください"
            "(面を選択すると、その輪郭が全部選ばれてしまいます)。"
        )

    start_idx = min(endpoints, key=lambda idx: (mat @ verts_by_index[idx].co).z)

    ordered = [verts_by_index[start_idx]]
    visited_edges = set()
    current_idx = start_idx
    while True:
        next_v = None
        for e in vert_to_edges[current_idx]:
            if e.index not in visited_edges:
                visited_edges.add(e.index)
                next_v = e.other_vert(verts_by_index[current_idx])
                break
        if next_v is None:
            break
        ordered.append(next_v)
        current_idx = next_v.index

    coords = [mat @ v.co for v in ordered]

    if owns_bm:
        bm.free()

    return coords


def _coords_ref_suffix(coords):
    """始点・終点の座標から、選択したラインごとに安定して一意になる名前サフィックスを作る。
    (同じオブジェクト内の別の辺を選んでも、名前が衝突して上書きされないようにするため)
    """
    def fmt(v):
        return f"{round(v.x,2)}_{round(v.y,2)}_{round(v.z,2)}".replace("-", "n").replace(".", "p")
    return f"{fmt(coords[0])}_{fmt(coords[-1])}"


def _instance_on_points_with_direction(pts_obj, tile_obj, direction):
    """1点(pts_obj)に、direction方向を向くよう回転を計算して、tile_objをインスタンス化する。
    棟瓦(_lay_tiles_along_coords)と同じ回転規則(世界Z軸基準)を使う。
    """
    z_axis = Vector((0, 0, 1))
    x_axis = direction.normalized()
    y_axis = z_axis.cross(x_axis).normalized()
    normal = x_axis.cross(y_axis).normalized()
    euler = _rotation_euler(x_axis, y_axis, normal)
    _instance_on_points(pts_obj, tile_obj, euler)


def _classify_ridge_endpoint(line_obj, end_point_world, ridge_direction):
    """大棟の端点(end_point_world)が、「ケラバ(軒線と直交)」に落ちるのか、
    「隅棟に収束する」のかを判定する。
    line_obj自身(またはその元になったRoofLines)の中から、端点に繋がっている
    他の辺を探し、大棟の向き(ridge_direction)との角度で判定する:
      - ほぼ直角(75度以上)に交わる辺がある → "KERAVA"(ケラバ、巴瓦が付く形状)
      - それ以外(斜めに繋がる辺がある、または何も見つからない) → "HIP"(隅棟に収束)
    """
    mat = line_obj.matrix_world
    mat_inv = mat.inverted()
    end_point_local = mat_inv @ end_point_world

    if line_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(line_obj.data)
        owns_bm = False
    else:
        bm = bmesh.new()
        bm.from_mesh(line_obj.data)
        owns_bm = True

    closest_vert = None
    closest_dist = float('inf')
    for v in bm.verts:
        d = (v.co - end_point_local).length
        if d < closest_dist:
            closest_dist = d
            closest_vert = v

    result = "HIP"  # 見つからない場合は、安全側(隅棟収束扱い)にしておく
    if closest_vert is not None:
        for e in closest_vert.link_edges:
            other = e.other_vert(closest_vert)
            vec_world = mat.to_3x3() @ (other.co - closest_vert.co)
            if vec_world.length < 1e-9:
                continue
            direction = vec_world.normalized()
            if abs(direction.dot(ridge_direction)) > 0.95:
                continue  # 大棟自身の延長は除外
            angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, abs(direction.dot(ridge_direction))))))
            if angle_deg >= 75.0:
                result = "KERAVA"
                break

    if owns_bm:
        bm.free()

    return result


def _place_ridge_end_caps(line_obj, coords, col, kawara_col):
    """大棟の両端(始点・終点)に、巴瓦・鬼瓦(役物)を配置する。
    役物のオブジェクトがコレクションに無ければ、何もせず静かに終わる
    (テストセットのような役物無しの瓦セットの、既存動作を壊さないため)。
    """
    roles = find_role_objects(col)
    tomoe_obj = roles.get("ridge_tomoe")
    oni_obj = roles.get("ridge_oni")
    if tomoe_obj is None and oni_obj is None:
        return []  # 役物が1つも無ければ何もしない(フォールバック)

    if len(coords) < 2:
        return []

    ridge_direction = (coords[-1] - coords[0]).normalized()
    created = []

    endpoints = [
        ("start", coords[0], -ridge_direction),   # 始点では、大棟の外側(逆方向)を向く
        ("end", coords[-1], ridge_direction),      # 終点では、大棟の進行方向を向く
    ]

    for label, point, outward_dir in endpoints:
        end_type = _classify_ridge_endpoint(line_obj, point, ridge_direction)

        # 回転(向き)は、棟本体の規則(x_axis=進行方向で180度、その逆で0度)に合わせるため、
        # 位置のシフト方向(outward_dir)とは逆向きを使う
        facing_dir = -outward_dir

        # 位置は常にシフト無し(端点そのまま)。瓦オブジェクト自身の原点に、
        # 施工順に応じた正しいオフセットが既に焼き込まれている前提のため、
        # コード側では一切ずらさない(微調整はオブジェクトの原点側で行う)。
        if end_type == "KERAVA":
            # ケラバ側: 巴瓦は端点そのまま。鬼瓦は、巴瓦がある場合、
            # 棟瓦本体の実効開始位置(巴瓦の幅の分だけ内側)に合わせる
            # (巴瓦の終点=棟瓦本体の始点、に鬼瓦が来るように)。
            if tomoe_obj is not None:
                obj = _make_point_cloud(f"{line_obj.name}_{label}_kawara_tomoe", [point], collection=kawara_col)
                _instance_on_points_with_direction(obj, tomoe_obj, facing_dir)
                created.append(obj)

                xs = [v.co.x for v in tomoe_obj.data.vertices]
                tomoe_width = max(xs) if xs else 0.0
                oni_point = point - outward_dir * tomoe_width
            else:
                oni_point = point

            if oni_obj is not None:
                obj = _make_point_cloud(f"{line_obj.name}_{label}_kawara_oni", [oni_point], collection=kawara_col)
                _instance_on_points_with_direction(obj, oni_obj, facing_dir)
                created.append(obj)
        else:
            # 隅棟収束側: 鬼瓦だけを、端点そのままに配置
            if oni_obj is not None:
                obj = _make_point_cloud(f"{line_obj.name}_{label}_kawara_oni", [point], collection=kawara_col)
                _instance_on_points_with_direction(obj, oni_obj, facing_dir)
                created.append(obj)

    return created


def _compute_effective_ridge_coords(line_obj, coords, col, pitch):
    """棟瓦本体を敷く前に、両端点(ケラバ/隅棟収束)を判定し、
    実際にピッチ計算へ使う「実効座標」を調整する。
      - ケラバ側で巴瓦がある場合: 巴瓦のX最大値の分だけ、内側に短縮する
      - 隅棟収束側の場合: 何もしない(生の座標のまま)。
        隅棟の収束部分の納まりは、設計・施工側の判断領域であり、
        ツール側で実在しない範囲まで自動で伸ばすべきではないため
        (伸ばすと、棟瓦の先端が屋根面から浮いてしまう)。
    戻り値: (実効coords, start_type, end_type)
    """
    if len(coords) < 2:
        return coords, None, None

    ridge_direction = (coords[-1] - coords[0]).normalized()
    roles = find_role_objects(col) if col is not None else {}
    tomoe_obj = roles.get("ridge_tomoe")

    tomoe_width = 0.0
    if tomoe_obj is not None:
        xs = [v.co.x for v in tomoe_obj.data.vertices]
        if xs:
            tomoe_width = max(xs)

    start_type = _classify_ridge_endpoint(line_obj, coords[0], ridge_direction)
    end_type = _classify_ridge_endpoint(line_obj, coords[-1], ridge_direction)

    effective_coords = list(coords)

    if start_type == "KERAVA" and tomoe_obj is not None:
        effective_coords[0] = coords[0] + ridge_direction * tomoe_width

    if end_type == "KERAVA" and tomoe_obj is not None:
        effective_coords[-1] = coords[-1] - ridge_direction * tomoe_width

    return effective_coords, start_type, end_type


def lay_line_tiles(line_obj, tile_obj, pitch, reverse=False):
    """ライン用オブジェクト(折れ線)に沿って等間隔にタイルを配置する(棟用: 終端に「終端部棟瓦」(180度回転の1枚)を追加)。
    reverse=True の場合、自動判定された始点・終点を入れ替える(壁際棟用)。
    コレクションに巴瓦・鬼瓦(役物)があれば、大棟の両端に自動で配置する(無ければ何もしない)。
    棟瓦本体を敷く前に、両端点(ケラバ/隅棟収束)を判定し、
    ケラバ側(巴瓦あり)は巴瓦の幅の分だけ短縮、隅棟収束側は棟ピッチ1本分延長してから、
    実際のピッチ計算・配置を行う。
    """
    coords = _get_ordered_polyline(line_obj)
    if reverse:
        coords = list(reversed(coords))

    col = bpy.context.scene.kawara_tool_props.tile_collection
    effective_coords, start_type, end_type = _compute_effective_ridge_coords(line_obj, coords, col, pitch)

    suffix = _coords_ref_suffix(coords)
    kawara_col = _get_or_create_kawara_collection(line_obj)
    created, count = _lay_tiles_along_coords(effective_coords, tile_obj, pitch, f"{line_obj.name}_{suffix}_kawara", collection=kawara_col)
    for o in created:
        o["kawara_pitch_mm"] = pitch * 1000.0
        _set_parent_keep_transform(o, line_obj)

    if col is not None:
        end_cap_objs = _place_ridge_end_caps(line_obj, coords, col, kawara_col)
        for o in end_cap_objs:
            _set_parent_keep_transform(o, line_obj)
        created += end_cap_objs
        count += len(end_cap_objs)

    return created, count


def _find_ridge_direction_at_endpoint(line_obj, end_point_world, exclude_direction):
    """隅棟ラインの終点に繋がっている他の辺の中から、一番水平に近い
    (Z方向の変化が一番小さい)辺の向きを、合流する棟ラインの向きとして推定する。
    見つからない場合は None を返す。
    """
    mat = line_obj.matrix_world
    mat_inv = mat.inverted()
    end_point_local = mat_inv @ end_point_world

    if line_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(line_obj.data)
        owns_bm = False
    else:
        bm = bmesh.new()
        bm.from_mesh(line_obj.data)
        owns_bm = True

    closest_vert = None
    closest_dist = float('inf')
    for v in bm.verts:
        d = (v.co - end_point_local).length
        if d < closest_dist:
            closest_dist = d
            closest_vert = v

    best_direction = None
    best_horizontality = -1.0
    if closest_vert is not None:
        for e in closest_vert.link_edges:
            other = e.other_vert(closest_vert)
            vec_world = mat.to_3x3() @ (other.co - closest_vert.co)
            if vec_world.length < 1e-9:
                continue
            direction = vec_world.normalized()
            # 隅棟自身の向き(逆向き含む)とほぼ同じものは、隅棟自身の延長とみなして除外
            if abs(direction.dot(exclude_direction)) > 0.95:
                continue
            horizontality = 1.0 - abs(direction.z)
            if horizontality > best_horizontality:
                best_horizontality = horizontality
                best_direction = direction

    if owns_bm:
        bm.free()

    return best_direction


def lay_verge_line_tiles(line_obj, tile_obj, pitch, side, reverse=False):
    """選択したラインに沿って、袖瓦を強制的に配置する(屋根面を選ぶ必要が無い版)。
    棟・隅棟と同じく、ラインの向き(始点はZが低い方)+世界Z軸だけから、
    屋根面の法線を使わずに正しい傾きを計算する
    (ケラバの3D方向自体に勾配の情報が含まれているため、これで面の法線と同じ結果になる)。
    side は "left" か "right"(どちらも配置の計算自体は同じで、瓦オブジェクトが違うだけ)。
    """
    coords = _get_ordered_polyline(line_obj)
    if reverse:
        coords = list(reversed(coords))
    if len(coords) < 2:
        raise ValueError("ラインには2点以上必要です。")

    kerava_dir = (coords[-1] - coords[0]).normalized()
    z_axis = Vector((0, 0, 1))
    cross1 = z_axis.cross(kerava_dir)
    if cross1.length < 1e-9:
        raise ValueError("ラインが鉛直に近すぎるため、傾きを計算できません。")
    derived_normal = kerava_dir.cross(cross1).normalized()

    suffix = _coords_ref_suffix(coords)
    kawara_col = _get_or_create_kawara_collection(line_obj)
    role_tag = "L" if side == "left" else "R"
    name_prefix = f"{line_obj.name}_{suffix}_kawara_verge_line_{role_tag}"

    created, count = _lay_verge_tiles_with_dynamic_rotation(
        coords, tile_obj, pitch, name_prefix, derived_normal, collection=kawara_col,
    )
    end_point = coords[-1]
    end_direction = (coords[-1] - coords[-2]).normalized() if len(coords) >= 2 else kerava_dir
    for o in created:
        o["kawara_line_object"] = line_obj.name
        o["kawara_ridge_end_point"] = tuple(end_point)
        o["kawara_ridge_end_direction"] = tuple(end_direction)
        o["kawara_pitch_mm"] = pitch * 1000.0
        _set_parent_keep_transform(o, line_obj)
    return created, count


def _place_hip_end_caps(line_obj, coords, col, kawara_col):
    """隅棟の軒先側の端点(始点、建物の角)に、隅棟巴瓦・隅棟鬼瓦(役物)を配置する。
    役物のオブジェクトがコレクションに無ければ、何もせず静かに終わる
    (役物無しの瓦セットの、既存動作を壊さないため)。
    棟側(終点、大棟との収束点)には何も配置しない
    (そちら側は大棟の_place_ridge_end_capsが鬼瓦を配置する前提のため)。
    """
    roles = find_role_objects(col)
    tomoe_obj = roles.get("hip_tomoe")
    oni_obj = roles.get("hip_oni")
    if tomoe_obj is None and oni_obj is None:
        return []

    if len(coords) < 2:
        return []

    point = coords[0]
    outward_dir = -(coords[1] - coords[0]).normalized()
    facing_dir = -outward_dir

    created = []
    if tomoe_obj is not None:
        obj = _make_point_cloud(f"{line_obj.name}_start_kawara_hip_tomoe", [point], collection=kawara_col)
        _instance_on_points_with_direction(obj, tomoe_obj, facing_dir)
        created.append(obj)

        xs = [v.co.x for v in tomoe_obj.data.vertices]
        tomoe_width = max(xs) if xs else 0.0
        oni_point = point - outward_dir * tomoe_width
    else:
        oni_point = point

    if oni_obj is not None:
        obj = _make_point_cloud(f"{line_obj.name}_start_kawara_hip_oni", [oni_point], collection=kawara_col)
        _instance_on_points_with_direction(obj, oni_obj, facing_dir)
        created.append(obj)

    return created


def lay_hip_line_tiles(line_obj, tile_obj, pitch, reverse=False):
    """隅棟ライン(折れ線)に沿って、始点基準の単純なピッチで隅棟瓦を配置する。
    生成したオブジェクトには、後でカットする時に選択し直さなくても済むよう、
    実際に使った終点の座標と向きを直接記録しておく
    (オブジェクト名だけだと、面の1辺だけ選択して使った場合に選択状態を復元できないため)。
    さらに、終点に繋がる棟ラインの向きも自動判別できれば一緒に記録しておく。
    reverse=True の場合、自動判定された始点・終点を入れ替える。
    コレクションに隅棟巴瓦・隅棟鬼瓦(役物)があれば、軒先側の端点に自動で配置する(無ければ何もしない)。
    隅棟巴瓦がある場合、棟と同様に、本体の実際の敷き始めを巴瓦の幅の分だけ内側にずらす
    (巴瓦の終点=本体の始点、に揃えるため)。
    """
    coords = _get_ordered_polyline(line_obj)
    if reverse:
        coords = list(reversed(coords))
    suffix = _coords_ref_suffix(coords)
    kawara_col = _get_or_create_kawara_collection(line_obj)

    col = bpy.context.scene.kawara_tool_props.tile_collection
    roles = find_role_objects(col) if col is not None else {}
    tomoe_obj = roles.get("hip_tomoe")
    tomoe_width = 0.0
    if tomoe_obj is not None:
        xs = [v.co.x for v in tomoe_obj.data.vertices]
        if xs:
            tomoe_width = max(xs)

    effective_coords = list(coords)
    if tomoe_width > 0 and len(coords) >= 2:
        # 隅棟巴瓦の"終点"と本体1枚目の"始点"を突き合わせて隙間・重なりを
        # 作らないため、本体の敷き始めを巴瓦の幅の分だけ内側(棟側)にずらす。
        # 棟(lay_line_tiles → _compute_effective_ridge_coords)のケラバ側と同じ考え方。
        hip_direction = (coords[1] - coords[0]).normalized()
        effective_coords[0] = coords[0] + hip_direction * tomoe_width

    created, count = _lay_tiles_along_coords_simple(effective_coords, tile_obj, pitch, f"{line_obj.name}_{suffix}_kawara_hip", collection=kawara_col)

    end_point = coords[-1]
    end_direction = (coords[-1] - coords[-2]).normalized()
    ridge_direction = _find_ridge_direction_at_endpoint(line_obj, end_point, end_direction)

    for o in created:
        o["kawara_line_object"] = line_obj.name
        o["kawara_hip_end_point"] = tuple(end_point)
        o["kawara_hip_end_direction"] = tuple(end_direction)
        o["kawara_pitch_mm"] = pitch * 1000.0
        _set_parent_keep_transform(o, line_obj)
        if ridge_direction is not None:
            o["kawara_hip_ridge_direction"] = tuple(ridge_direction)

    if col is not None:
        end_cap_objs = _place_hip_end_caps(line_obj, coords, col, kawara_col)
        for o in end_cap_objs:
            _set_parent_keep_transform(o, line_obj)
        created += end_cap_objs
        count += len(end_cap_objs)

    return created, count


def cut_ridge_tiles_to_end(ridge_obj):
    """棟瓦(順方向のグループ)を、終点で実際にカットする(はみ出た部分を削除)。
    配置時に記録された終点座標・向き(kawara_ridge_end_point / kawara_ridge_end_direction)を使う。
    対応する終端部棟瓦(180度回転の1枚)が見つかれば、それも一緒に実メッシュ化しておく
    (点群+モディファイアのままだと、アウトライナー上でマテリアルが未設定に見えて紛らわしいため)。
    """
    if "kawara_ridge_end_point" not in ridge_obj or "kawara_ridge_end_direction" not in ridge_obj:
        raise ValueError(f"「{ridge_obj.name}」には終端カット用の情報が記録されていません。")

    end_point = Vector(ridge_obj["kawara_ridge_end_point"])
    direction = Vector(ridge_obj["kawara_ridge_end_direction"])
    _cut_mesh_with_planes(ridge_obj, [(end_point, direction)])

    sibling = _find_ridge_end_tile_sibling(ridge_obj)
    if sibling is not None and sibling.modifiers:
        bpy.context.view_layer.objects.active = sibling
        bpy.ops.object.select_all(action='DESELECT')
        sibling.select_set(True)
        bpy.ops.object.convert(target='MESH')


def _cut_mesh_with_plane_capped(obj, point, plane_no):
    """objを実メッシュ化したうえで、1つの平面でbisectし、外側を削除したうえで、
    切断面(開いた断面)に蓋の面を張る。
    """
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.ops.object.convert(target='MESH')

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    ret = bmesh.ops.bisect_plane(
        bm, geom=geom, plane_co=point, plane_no=plane_no,
        clear_outer=True, clear_inner=False,
    )
    cut_edges = [g for g in ret['geom_cut'] if isinstance(g, bmesh.types.BMEdge)]
    if cut_edges:
        try:
            bmesh.ops.edgenet_fill(bm, edges=cut_edges)
        except Exception:
            pass  # 断面が複雑すぎて張れない場合は、蓋なしのまま諦める

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def cut_verge_line_tiles_to_end(verge_obj):
    """ラインベースで配置した袖瓦(kawara_ridge_end_point系のタグを流用)を、
    終点で垂直にカットし、切断面に蓋をする。
    """
    if "kawara_ridge_end_point" not in verge_obj:
        raise ValueError(f"「{verge_obj.name}」には終端カット用の情報が記録されていません。")

    end_point = Vector(verge_obj["kawara_ridge_end_point"])
    direction = Vector(verge_obj["kawara_ridge_end_direction"])
    _cut_mesh_with_plane_capped(verge_obj, end_point, direction)


def cut_hip_tiles_to_ridge(hip_obj, line_obj=None):
    """隅棟瓦を、合流先の棟ラインの延長線でカットする。
    配置時に記録された終点座標(kawara_hip_end_point)が優先され、
    棟の向き(kawara_hip_ridge_direction)が判別できていればそれを使い、
    判別できていなければ隅棟自身の向き(kawara_hip_end_direction)で代用する。
    どちらも記録されていない場合のみ、line_obj から現在の選択状態を見て計算する(従来動作)。
    """
    if "kawara_hip_end_point" in hip_obj:
        end_point = Vector(hip_obj["kawara_hip_end_point"])
        hip_dir = Vector(hip_obj["kawara_hip_end_direction"])
        if "kawara_hip_ridge_direction" in hip_obj:
            ridge_dir = Vector(hip_obj["kawara_hip_ridge_direction"])
            # 棟の延長線を含む縦の面でカットしたいので、法線は
            # 「棟の向きに直角、かつ水平」にする(棟の向きそのものだと棟と垂直な面になってしまう)
            z_axis = Vector((0, 0, 1))
            plane_no = z_axis.cross(ridge_dir)
            if plane_no.length < 1e-9:
                plane_no = hip_dir
            else:
                plane_no = plane_no.normalized()
                # はみ出た側(隅棟が延びていく向き)が必ず「外側」になるよう符号を揃える
                if plane_no.dot(hip_dir) < 0:
                    plane_no = -plane_no
        else:
            plane_no = hip_dir
        _cut_mesh_with_planes(hip_obj, [(end_point, plane_no)])
        return

    if line_obj is None:
        raise ValueError("終点の情報が記録されていないため、隅棟ラインオブジェクトが必要です。")

    coords = _get_ordered_polyline(line_obj)
    if len(coords) < 2:
        raise ValueError("ラインには2点以上必要です。")
    end_point = coords[-1]
    direction = (coords[-1] - coords[-2]).normalized()
    _cut_mesh_with_planes(hip_obj, [(end_point, direction)])


def refresh_spec_from_collection_props(col, context=None):
    """コレクションのカスタムプロパティ(kawara_hataraki_habaなど)、
    または(あれば)コレクション自身のkawara_spec(ローカルの場合のみ編集可能)から、
    シーン側の作業用スペック(kawara_working_spec、常にローカルで編集可能)へ値を反映する。
    無い項目はそのまま(前の値を維持)。
    """
    context = context or bpy.context
    working_spec = context.scene.kawara_working_spec

    # まずコレクション自身のkawara_spec(ローカルなら編集済みの値が入っている)があれば、それを使う
    if col.library is None:
        col_spec = col.kawara_spec
        for field in ("hataraki_haba", "hataraki_nagasa", "mune_pitch", "sumi_mune_pitch", "chousei_sunpo"):
            setattr(working_spec, field, getattr(col_spec, field))

    # カスタムプロパティがあれば、それで上書きする(Linkされたコレクションでも読み取りは可能)
    # chidori(千鳥敷き)は、他の項目と違って「無ければ前の値を維持」にしてしまうと、
    # 千鳥ONのセットから別のセットに切り替えた際、千鳥設定を持たないはずのセットにまで
    # 千鳥がそのまま引き継がれてしまう。敷き方そのものを切り替える値なので、
    # 明示的な指定が無ければ必ずFalse(通常敷き)に戻す。
    working_spec.chidori = bool(col.get("kawara_chidori", False))

    mapping = {
        "kawara_hataraki_haba": "hataraki_haba",
        "kawara_hataraki_nagasa": "hataraki_nagasa",
        "kawara_mune_pitch": "mune_pitch",
        "kawara_sumi_mune_pitch": "sumi_mune_pitch",
        "kawara_chousei_sunpo": "chousei_sunpo",
        "kawara_eave_overhang_mm": "eave_overhang",
        "kawara_kerava_overhang_mm": "kerava_overhang",
    }
    for prop_key, spec_field in mapping.items():
        if prop_key in col:
            setattr(working_spec, spec_field, col[prop_key])


_last_tile_collection_name = None  # 直前に処理したコレクション名(同じ物を選び直した時の再処理を防ぐ)


def _update_tile_collection(self, context):
    # 同じコレクションを選び直しただけ(値が変わっていない)場合は、
    # 寸法スペックの再読み込みをスキップする。
    # (プロパティの再選択自体はBlender側の挙動なので防げないが、
    #  こちら側の後処理を不要に繰り返さないための保険)
    global _last_tile_collection_name
    name = self.tile_collection.name if self.tile_collection is not None else None
    if name == _last_tile_collection_name:
        return
    _last_tile_collection_name = name
    if self.tile_collection is not None:
        refresh_spec_from_collection_props(self.tile_collection, context)


def _list_kawara_set_files():
    """KawaraSet フォルダ内の .blend ファイル名一覧を返す。"""
    folder = _get_kawara_set_folder()
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".blend"))


_kawara_set_file_items_cache = []  # EnumPropertyの動的アイテムは、Python側で参照を保持しておかないと
                                    # ガベージコレクションされて文字化けする(Blenderの既知の挙動)ため、
                                    # 戻り値をモジュールレベルの変数に保持しておく。


def _kawara_set_file_items(self, context):
    global _kawara_set_file_items_cache
    files = _list_kawara_set_files()
    if not files:
        _kawara_set_file_items_cache = [("NONE", "(ファイルが見つかりません)", "")]
    else:
        _kawara_set_file_items_cache = [(f, f, "") for f in files]
    return _kawara_set_file_items_cache


class KawaraToolProps(bpy.types.PropertyGroup):
    tile_collection: PointerProperty(
        name="瓦コレクション", type=bpy.types.Collection,
        description="平・軒・袖(左右)・棟・隅棟のオブジェクトが入ったコレクションを指定",
        update=_update_tile_collection,
    )
    ridge_reverse: BoolProperty(
        name="壁際棟(向きを逆にする)",
        description="棟の始点・終点を入れ替える(壁際棟: 棟が壁にぶつかって終わる形状の場合に使用)",
        default=False,
    )
    random_seed: bpy.props.IntProperty(
        name="ランダムシード",
        description="瓦ごとの色ムラなどに使うランダムパターンの種。値を変える(振り直す)と模様が変わる。",
        default=0,
    )
    kawara_set_file: bpy.props.EnumProperty(
        name="読み込むファイル",
        description="KawaraSetフォルダ内の、読み込みたい.blendファイルを選択",
        items=_kawara_set_file_items,
    )



class KAWARA_OT_refresh_spec(bpy.types.Operator):
    """コレクションのカスタムプロパティから寸法スペックを読み込み直す"""
    bl_idname = "kawara.refresh_spec"
    bl_label = "コレクションの属性から寸法を読み込み直す"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.kawara_tool_props.tile_collection is not None

    def execute(self, context):
        col = context.scene.kawara_tool_props.tile_collection
        refresh_spec_from_collection_props(col, context)
        self.report({'INFO'}, "コレクションの属性から寸法を読み込みました")
        return {'FINISHED'}


def _reroll_seed_new_value(context):
    """新しいランダムシード値を生成してシーンプロパティに保存するだけ
    (既存の配置済みオブジェクトには触らない)。以降の新規配置に使われる。
    """
    import random
    props = context.scene.kawara_tool_props
    props.random_seed = random.randint(0, 1_000_000)


def _apply_seed_to_existing_tiles(context):
    """今のランダムシード値を、既に配置済みの瓦(KawaraTileLayモディファイア持ち)
    すべてに反映し、即座に画面に反映させる。戻り値: 反映したオブジェクト数。
    """
    props = context.scene.kawara_tool_props
    ng = bpy.data.node_groups.get("KawaraTileLay")
    seed_id = None
    if ng is not None:
        for item in ng.interface.items_tree:
            if item.name == "Random Seed" and item.item_type == 'SOCKET':
                seed_id = item.identifier
                break

    updated = 0
    if seed_id:
        for obj in bpy.data.objects:
            touched = False
            for mod in obj.modifiers:
                if mod.type == 'NODES' and mod.node_group is not None and mod.node_group.name.startswith("KawaraTileLay"):
                    try:
                        mod[seed_id] = props.random_seed
                        touched = True
                        updated += 1
                    except Exception:
                        pass
            if touched:
                obj.update_tag()

    context.view_layer.update()
    return updated


class KAWARA_OT_reroll_random_seed(bpy.types.Operator):
    """ランダムシードを振り直し、既に配置済みの瓦にも即座に反映する
    (瓦ごとの色ムラなどのパターンを変えたい場合に使う)。
    """
    bl_idname = "kawara.reroll_random_seed"
    bl_label = "ランダムを振り直す"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _reroll_seed_new_value(context)
        updated = _apply_seed_to_existing_tiles(context)
        self.report({'INFO'}, f"ランダムシードを振り直しました({updated}個のオブジェクトに反映)")
        return {'FINISHED'}


class KAWARA_OT_load_kawara_sets(bpy.types.Operator):
    """選択した.blendファイルから、まだシーンに無い瓦セットをLinkで読み込む"""
    bl_idname = "kawara.load_kawara_sets"
    bl_label = "選択したファイルを読み込む"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.kawara_tool_props.kawara_set_file != "NONE"

    def execute(self, context):
        fname = context.scene.kawara_tool_props.kawara_set_file
        loaded = _load_kawara_set_file(context, fname)
        if not loaded:
            self.report({'INFO'}, "新しく読み込む瓦セットはありませんでした(既に読み込み済みです)。")
            return {'FINISHED'}
        self.report({'INFO'}, f"読み込みました: {' / '.join(loaded)}")
        return {'FINISHED'}


class KAWARA_OT_lay_roof(bpy.types.Operator):
    """選択中の屋根面(平面ポリゴン)に、平瓦→軒瓦→袖瓦の順で敷く"""
    bl_idname = "kawara.lay_roof"
    bl_label = "屋根面に瓦を敷く"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.kawara_tool_props
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and props.tile_collection is not None
        )

    def execute(self, context):
        face_obj = context.active_object
        props = context.scene.kawara_tool_props
        col = props.tile_collection
        _reroll_seed_new_value(context)

        try:
            created_flat, n_flat = lay_flat_tiles(face_obj, col)
            created_eave, n_eave = lay_eave_tiles(face_obj, col)
            created_vl, n_vl = lay_verge_tiles(face_obj, col, "left")
            created_vr, n_vr = lay_verge_tiles(face_obj, col, "right")
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        created = created_flat + created_eave + created_vl + created_vr
        # 編集モードで作業中だった場合は、アクティブオブジェクトを変えると
        # 強制的にオブジェクトモードへ戻されてしまうため、その場合は変更しない
        if face_obj.mode != 'EDIT':
            for o in created:
                o.select_set(True)
            if created:
                context.view_layer.objects.active = created[0]

        total = n_flat + n_eave + n_vl + n_vr
        self.report(
            {'INFO'},
            f"瓦を敷きました(平{n_flat} / 軒{n_eave} / 左袖{n_vl} / 右袖{n_vr} / 合計{total}枚)",
        )
        return {'FINISHED'}


class KAWARA_OT_lay_ridge(bpy.types.Operator):
    """選択中のライン(2頂点以上)に沿って棟瓦を等間隔に配置する"""
    bl_idname = "kawara.lay_ridge"
    bl_label = "棟に瓦を敷く"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.kawara_tool_props
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and props.tile_collection is not None
        )

    def execute(self, context):
        line_obj = context.active_object
        props = context.scene.kawara_tool_props
        col = props.tile_collection
        roles = find_role_objects(col)
        ridge_obj = roles.get("ridge")
        if ridge_obj is None:
            self.report({'ERROR'}, "コレクション内に「棟」瓦が見つかりません。")
            return {'CANCELLED'}

        _reroll_seed_new_value(context)
        try:
            created, count = lay_line_tiles(line_obj, ridge_obj, context.scene.kawara_working_spec.mune_pitch / 1000.0, reverse=props.ridge_reverse)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if line_obj.mode != 'EDIT':
            for o in created:
                o.select_set(True)
            if created:
                context.view_layer.objects.active = created[0]
        self.report({'INFO'}, f"棟瓦を敷きました({count}個、等間隔)")
        return {'FINISHED'}


class KAWARA_OT_lay_hip(bpy.types.Operator):
    """選択中のライン(2頂点以上)に沿って隅棟瓦を等間隔に配置する"""
    bl_idname = "kawara.lay_hip"
    bl_label = "隅棟に瓦を敷く"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.kawara_tool_props
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and props.tile_collection is not None
        )

    def execute(self, context):
        line_obj = context.active_object
        col = context.scene.kawara_tool_props.tile_collection
        roles = find_role_objects(col)
        hip_obj = roles.get("hip")
        if hip_obj is None:
            self.report({'ERROR'}, "コレクション内に「隅棟」瓦が見つかりません。")
            return {'CANCELLED'}

        _reroll_seed_new_value(context)
        try:
            created, count = lay_hip_line_tiles(line_obj, hip_obj, context.scene.kawara_working_spec.sumi_mune_pitch / 1000.0)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if line_obj.mode != 'EDIT':
            for o in created:
                o.select_set(True)
            if created:
                context.view_layer.objects.active = created[0]
        self.report({'INFO'}, f"隅棟瓦を敷きました({count}個、等間隔)")
        return {'FINISHED'}


class KAWARA_OT_lay_verge_line_left(bpy.types.Operator):
    """選択中のライン(2頂点以上)に沿って、左袖瓦を強制的に配置する(屋根面を選ぶ必要が無い)"""
    bl_idname = "kawara.lay_verge_line_left"
    bl_label = "左袖設置 (ラインを選択)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.kawara_tool_props
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and props.tile_collection is not None
        )

    def execute(self, context):
        line_obj = context.active_object
        props = context.scene.kawara_tool_props
        col = props.tile_collection
        roles = find_role_objects(col)
        verge_obj = roles.get("verge_left")
        if verge_obj is None:
            self.report({'ERROR'}, "コレクション内に「左袖」瓦が見つかりません。")
            return {'CANCELLED'}

        _reroll_seed_new_value(context)
        try:
            created, count = lay_verge_line_tiles(
                line_obj, verge_obj, context.scene.kawara_working_spec.hataraki_nagasa / 1000.0, "left",
                reverse=props.ridge_reverse,
            )
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if line_obj.mode != 'EDIT':
            for o in created:
                o.select_set(True)
            if created:
                context.view_layer.objects.active = created[0]
        self.report({'INFO'}, f"左袖瓦を敷きました({count}個、等間隔)")
        return {'FINISHED'}


class KAWARA_OT_lay_verge_line_right(bpy.types.Operator):
    """選択中のライン(2頂点以上)に沿って、右袖瓦を強制的に配置する(屋根面を選ぶ必要が無い)"""
    bl_idname = "kawara.lay_verge_line_right"
    bl_label = "右袖設置 (ラインを選択)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.kawara_tool_props
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
            and props.tile_collection is not None
        )

    def execute(self, context):
        line_obj = context.active_object
        props = context.scene.kawara_tool_props
        col = props.tile_collection
        roles = find_role_objects(col)
        verge_obj = roles.get("verge_right")
        if verge_obj is None:
            self.report({'ERROR'}, "コレクション内に「右袖」瓦が見つかりません。")
            return {'CANCELLED'}

        _reroll_seed_new_value(context)
        try:
            created, count = lay_verge_line_tiles(
                line_obj, verge_obj, context.scene.kawara_working_spec.hataraki_nagasa / 1000.0, "right",
                reverse=props.ridge_reverse,
            )
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if line_obj.mode != 'EDIT':
            for o in created:
                o.select_set(True)
            if created:
                context.view_layer.objects.active = created[0]
        self.report({'INFO'}, f"右袖瓦を敷きました({count}個、等間隔)")
        return {'FINISHED'}


class KAWARA_OT_cut_verge_line_overhang(bpy.types.Operator):
    """選択中の(ラインベースで配置した)袖瓦オブジェクトを、記録された終点で
    垂直にカットし、切断面に蓋をする"""
    bl_idname = "kawara.cut_verge_line_overhang"
    bl_label = "はみ出た袖瓦を終端でカット"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        verge_obj = context.active_object
        try:
            cut_verge_line_tiles_to_end(verge_obj)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        self.report({'INFO'}, f"「{verge_obj.name}」を終端でカットしました")
        return {'FINISHED'}


class KAWARA_OT_cut_hip_overhang(bpy.types.Operator):
    """選択中の隅棟瓦オブジェクトを、隅棟ラインの終点(棟との合流点)でカットする。
    対象のラインは、配置時に記録されたタグから自動で判別する
    (見つからない場合のみ、line_object を手動指定する)。
    """
    bl_idname = "kawara.cut_hip_overhang"
    bl_label = "はみ出た隅棟瓦を棟側でカット"
    bl_options = {'REGISTER', 'UNDO'}

    line_object: StringProperty(name="対象の隅棟ラインオブジェクト名(自動判別できない場合のみ指定)", default="")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        hip_obj = context.active_object

        line_name = self.line_object or hip_obj.get("kawara_line_object")
        line_obj = bpy.data.objects.get(line_name) if line_name else None

        try:
            cut_hip_tiles_to_ridge(hip_obj, line_obj)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        self.report({'INFO'}, f"「{hip_obj.name}」を棟側でカットしました")
        return {'FINISHED'}


class KAWARA_OT_cut_ridge_overhang(bpy.types.Operator):
    """選択中の棟瓦オブジェクトを、記録された終点でカットする(はみ出た部分を削除)"""
    bl_idname = "kawara.cut_ridge_overhang"
    bl_label = "はみ出た棟瓦を終端でカット"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        ridge_obj = context.active_object
        try:
            cut_ridge_tiles_to_end(ridge_obj)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        self.report({'INFO'}, f"「{ridge_obj.name}」を終端でカットしました")
        return {'FINISHED'}


def _find_ridge_end_tile_sibling(ridge_obj):
    """棟瓦の「順方向」オブジェクト(終端カット用タグ付き)から、
    同じライン由来で、終端タグの付いていない「終端部棟瓦」オブジェクトを探す。
    """
    m = re.match(r'^(.*)_(\d+)$', ridge_obj.name)
    if not m:
        return None
    prefix = m.group(1)
    for obj in bpy.data.objects:
        if obj is ridge_obj or obj.type != 'MESH':
            continue
        if not obj.name.startswith(prefix + "_"):
            continue
        suffix = obj.name[len(prefix) + 1:]
        if suffix.isdigit() and "kawara_ridge_end_point" not in obj:
            return obj
    return None


class KAWARA_OT_delete_ridge_end_tile(bpy.types.Operator):
    """終端部棟瓦(180度回転の1枚)オブジェクトを削除する。
    棟の「順方向」オブジェクト(終端カット用タグ付き)を選んだ場合は、
    対応する終端部棟瓦を自動的に探して削除する。
    それ以外の場合は、選択中のオブジェクトをそのまま削除する。
    """
    bl_idname = "kawara.delete_ridge_end_tile"
    bl_label = "終端部棟瓦を削除"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        targets = []
        for obj in list(context.selected_objects):
            if obj.type != 'MESH':
                continue
            if "kawara_ridge_end_point" in obj:
                sibling = _find_ridge_end_tile_sibling(obj)
                if sibling is not None:
                    targets.append(sibling)
                else:
                    self.report({'WARNING'}, f"「{obj.name}」に対応する終端部棟瓦が見つかりませんでした。")
            else:
                targets.append(obj)

        removed = []
        seen = set()
        for obj in targets:
            if obj.name in seen:
                continue
            seen.add(obj.name)
            name = obj.name
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            removed.append(name)

        if not removed:
            self.report({'WARNING'}, "削除するオブジェクトが見つかりませんでした。")
            return {'CANCELLED'}

        self.report({'INFO'}, f"削除しました: {' / '.join(removed)}")
        return {'FINISHED'}


def draw_kawara_panel(layout, context):
    """瓦アドオンのパネル内容を描く(タブ統合パネルからも、単独パネルからも呼べる)。"""
    props = context.scene.kawara_tool_props

    box = layout.box()
    box.prop(props, "kawara_set_file", text="")
    box.operator(KAWARA_OT_load_kawara_sets.bl_idname, icon='IMPORT')
    col0 = box.column(align=True)
    col0.scale_y = 0.8
    col0.label(text="KawaraSetフォルダの.blendから選んだ瓦セットをLinkで読み込む")

    box = layout.box()
    box.label(text="1. 瓦コレクションを指定", icon='OUTLINER_COLLECTION')
    box.prop(props, "tile_collection", text="")
    col = box.column(align=True)
    col.scale_y = 0.8
    col.label(text="名前に 平/軒瓦/右袖/左袖/棟/隅棟 を含めてください")

    if props.tile_collection:
        spec = context.scene.kawara_working_spec
        box = layout.box()
        box.label(text="寸法スペック", icon='PROPERTIES')
        box.operator(KAWARA_OT_refresh_spec.bl_idname, icon='FILE_REFRESH')
        colf = box.column(align=True)
        colf.prop(spec, "hataraki_haba")
        colf.prop(spec, "hataraki_nagasa")
        colf.prop(spec, "mune_pitch")
        colf.prop(spec, "sumi_mune_pitch")
        colf.prop(spec, "chousei_sunpo")

        box = layout.box()
        box.label(text="2. 対象を選んで配置", icon='MOD_ARRAY')
        box.operator(KAWARA_OT_lay_roof.bl_idname, text="平瓦・軒瓦・袖瓦の配置 (屋根面を選択))")
        row = box.row(align=True)
        row.operator(KAWARA_OT_lay_ridge.bl_idname, text="棟瓦の配置 (ラインを選択)")
        row.prop(props, "ridge_reverse", text="", icon='ARROW_LEFTRIGHT')
        box.operator(KAWARA_OT_lay_hip.bl_idname, text="隅棟瓦の配置 (ラインを選択)")
        row2 = box.row(align=True)
        row2.operator(KAWARA_OT_lay_verge_line_left.bl_idname)
        row2.operator(KAWARA_OT_lay_verge_line_right.bl_idname)

        box = layout.box()
        box.label(text="3. はみ出しをカット (任意)", icon='MOD_BOOLEAN')
        box.operator("kawara.cut_all_overhang", text="選択中のはみ出しをまとめてカット")

        col3 = box.column(align=True)
        col3.scale_y = 0.8
        col3.label(text="個別にカットしたい場合(瓦オブジェクトを選択):")
        colc = box.column(align=True)
        row = colc.row(align=True)
        row.operator(KAWARA_OT_cut_flat_overhang.bl_idname, text="平瓦をカット")
        row.operator(KAWARA_OT_cut_eave_overhang.bl_idname, text="軒瓦をカット")
        row = colc.row(align=True)
        row.operator(KAWARA_OT_cut_ridge_overhang.bl_idname, text="棟瓦をカット")
        row.operator(KAWARA_OT_cut_hip_overhang.bl_idname, text="隅棟瓦をカット")
        colc.operator(KAWARA_OT_delete_ridge_end_tile.bl_idname, text="終端部棟瓦を削除")
        colc.operator(KAWARA_OT_cut_verge_line_overhang.bl_idname, text="袖瓦をカット(ライン配置分)")

    box = layout.box()
    row = box.row(align=True)
    row.operator(KAWARA_OT_reroll_random_seed.bl_idname, icon='FILE_REFRESH')
    row.label(text=str(props.random_seed))


# ---------------------------------------------------------------------------
# はみ出た平瓦を屋根の輪郭で物理的にカットする
# ---------------------------------------------------------------------------

def _outward_plane(p1, p2, face_normal, centroid):
    """辺(p1→p2)から、面の外側を向く切断面(点, 法線)を作る。"""
    edge_dir = (p2 - p1)
    if edge_dir.length < 1e-9:
        return None
    edge_dir = edge_dir.normalized()
    plane_no = face_normal.cross(edge_dir).normalized()
    if plane_no.dot(centroid - p1) > 0:
        plane_no = -plane_no
    return (p1, plane_no)


def _cut_mesh_with_planes(obj, planes):
    """obj を実メッシュ化したうえで、planes=[(点, 外向き法線), ...] で順にbisectし、
    それぞれの外側(はみ出た部分)を削除する。
    """
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.ops.object.convert(target='MESH')

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    for point, plane_no in planes:
        geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
        bmesh.ops.bisect_plane(
            bm, geom=geom, plane_co=point, plane_no=plane_no,
            clear_outer=True, clear_inner=False,
        )
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _compute_normal(verts):
    n = len(verts)
    normal = Vector((0.0, 0.0, 0.0))
    for i in range(n):
        v_curr = verts[i]
        v_next = verts[(i + 1) % n]
        normal.x += (v_curr.y - v_next.y) * (v_curr.z + v_next.z)
        normal.y += (v_curr.z - v_next.z) * (v_curr.x + v_next.x)
        normal.z += (v_curr.x - v_next.x) * (v_curr.y + v_next.y)
    normal.normalize()
    return normal


def cut_flat_tiles_to_face(flat_obj):
    """flat_obj(平瓦のインスタンス群)を、記録された元の面の輪郭でカットする。
    軒先側の辺(辺0: verts[0]→verts[1])だけは常に除外する。
    軒瓦がある構成では平瓦はそもそも軒先の行まで届かないため実害は無いが、
    軒瓦を使わない構成(平瓦が軒先ぎりぎりの行まで敷かれる)では、この辺で
    カットしてしまうと平瓦本来の軒先側のはみ出し(軒瓦が無い分の見た目)が
    直線でスパッと切られてしまうため、軒瓦・袖瓦のカット(cut_eave_tiles_to_kerava /
    cut_verge_tiles_to_face)と同じ考え方で、ここでも軒先の辺は使わない。
    """
    verts, _ = _get_face_verts_for_tile(flat_obj)
    n = len(verts)
    centroid = sum(verts, Vector()) / n
    normal = _compute_normal(verts)

    eave_edge = (0, 1)

    planes = []
    for i in range(n):
        edge = (i, (i + 1) % n)
        if edge == eave_edge:
            continue
        pl = _outward_plane(verts[edge[0]], verts[edge[1]], normal, centroid)
        if pl:
            planes.append(pl)

    _cut_mesh_with_planes(flat_obj, planes)


def cut_eave_tiles_to_kerava(eave_obj):
    """軒瓦を、左右のケラバ(妻)延長ラインだけでカットする。
    軒先・棟側の辺は使わない(軒瓦の引っ掛けタブなどの意図的なはみ出しを残すため)。
    """
    verts, _ = _get_face_verts_for_tile(eave_obj)
    n = len(verts)
    centroid = sum(verts, Vector()) / n
    normal = _compute_normal(verts)

    left_edge = (verts[-1], verts[0])
    right_edge = (verts[1], verts[2 % n])

    planes = []
    for p1, p2 in (left_edge, right_edge):
        pl = _outward_plane(p1, p2, normal, centroid)
        if pl:
            planes.append(pl)

    _cut_mesh_with_planes(eave_obj, planes)


def cut_verge_tiles_to_face(verge_obj, side):
    """袖瓦を、棟側の辺だけでカットする。
    軒先側(辺0)とケラバの辺自体は使わない
    (袖瓦のオブジェクト側で軒先は既にぴったり原点合わせされている前提のため)。
    """
    verts, _ = _get_face_verts_for_tile(verge_obj)
    n = len(verts)
    centroid = sum(verts, Vector()) / n
    normal = _compute_normal(verts)

    own_kerava_edge = (n - 1, 0) if side == "left" else (1, 2 % n)
    exclude_edges = {(0, 1), own_kerava_edge}

    planes = []
    for i in range(n):
        edge = (i, (i + 1) % n)
        if edge in exclude_edges:
            continue
        pl = _outward_plane(verts[edge[0]], verts[edge[1]], normal, centroid)
        if pl:
            planes.append(pl)

    if not planes:
        return

    _cut_mesh_with_planes(verge_obj, planes)


def cut_all_tiles_by_face_ref(face_obj_name, face_index):
    """(元の屋根面オブジェクト名, 面インデックス)から、対応する瓦オブジェクトを
    すべて探してカットする。戻り値: 実際にカットした役割名のリスト。
    """
    target_index = face_index if face_index is not None else -1
    candidates = [
        o for o in bpy.data.objects
        if o.get("kawara_face_object") == face_obj_name and o.get("kawara_face_index", -1) == target_index
    ]

    done = []
    role_labels = {"flat": "平瓦", "eave": "軒瓦", "verge_left": "左袖瓦", "verge_right": "右袖瓦"}
    for obj in candidates:
        role = obj.get("kawara_role")
        if role == "flat":
            cut_flat_tiles_to_face(obj)
        elif role == "eave":
            cut_eave_tiles_to_kerava(obj)
        elif role == "verge_left":
            cut_verge_tiles_to_face(obj, "left")
        elif role == "verge_right":
            cut_verge_tiles_to_face(obj, "right")
        else:
            continue
        done.append(role_labels.get(role, role))

    return done


def cut_all_tiles_to_face(face_obj):
    """指定した屋根面(オブジェクト、または編集モードで選択中の面)に対応する
    平瓦・軒瓦・左右袖瓦をまとめてカットする(面を選び直す従来の使い方)。
    """
    verts, face_index, _edge_bnd = _get_face_verts(face_obj)
    return cut_all_tiles_by_face_ref(face_obj.name, face_index)


class KAWARA_OT_cut_flat_overhang(bpy.types.Operator):
    """選択中の平瓦オブジェクトを、記録された元の面の輪郭(全辺)で実際にカットする"""
    bl_idname = "kawara.cut_flat_overhang"
    bl_label = "はみ出た平瓦をカット"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        flat_obj = context.active_object
        try:
            cut_flat_tiles_to_face(flat_obj)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        self.report({'INFO'}, f"「{flat_obj.name}」を屋根の輪郭でカットしました")
        return {'FINISHED'}


class KAWARA_OT_cut_all_overhang(bpy.types.Operator):
    """選択中のオブジェクト(屋根面・平瓦・軒瓦・袖瓦・隅棟瓦、複数選択可)をまとめてカットする"""
    bl_idname = "kawara.cut_all_overhang"
    bl_label = "選択中のはみ出しをまとめてカット"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        selected = list(context.selected_objects) or (
            [context.active_object] if context.active_object else []
        )
        if not selected:
            self.report({'WARNING'}, "対象のオブジェクトを選択してください。")
            return {'CANCELLED'}

        done_all = []
        processed_face_groups = set()

        for obj in selected:
            if obj.type != 'MESH':
                continue
            try:
                if "kawara_hip_end_point" in obj:
                    # 隅棟瓦
                    cut_hip_tiles_to_ridge(obj)
                    done_all.append(f"隅棟瓦({obj.name})")
                elif "kawara_ridge_end_point" in obj:
                    # 棟瓦
                    cut_ridge_tiles_to_end(obj)
                    done_all.append(f"棟瓦({obj.name})")
                elif obj.get("kawara_face_object") is not None:
                    # 平瓦・軒瓦・袖瓦(配置済みの瓦オブジェクト): 記録された参照からグループを探す
                    face_index = obj.get("kawara_face_index", -1)
                    face_index = None if face_index is None or face_index < 0 else face_index
                    key = (obj["kawara_face_object"], face_index)
                    if key in processed_face_groups:
                        continue
                    processed_face_groups.add(key)
                    done_all.extend(cut_all_tiles_by_face_ref(obj["kawara_face_object"], face_index))
                else:
                    # 屋根面(またはその面)そのものが選ばれている場合
                    done_all.extend(cut_all_tiles_to_face(obj))
            except Exception as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}

        if not done_all:
            self.report({'WARNING'}, "対応する瓦オブジェクトが見つかりませんでした(屋根面か、配置済みの瓦オブジェクトを選択してください)")
            return {'CANCELLED'}

        self.report({'INFO'}, f"カットしました: {' / '.join(done_all)}")
        return {'FINISHED'}


class KAWARA_OT_cut_eave_overhang(bpy.types.Operator):
    """選択中の軒瓦オブジェクトを、左右のケラバ延長ラインだけでカットする"""
    bl_idname = "kawara.cut_eave_overhang"
    bl_label = "はみ出た軒瓦をケラバでカット"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        eave_obj = context.active_object
        try:
            cut_eave_tiles_to_kerava(eave_obj)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        self.report({'INFO'}, f"「{eave_obj.name}」をケラバでカットしました")
        return {'FINISHED'}


classes = (
    KawaraPatternSpec,
    KawaraToolProps,
    KAWARA_OT_reroll_random_seed,
    KAWARA_OT_load_kawara_sets,
    KAWARA_OT_refresh_spec,
    KAWARA_OT_lay_roof,
    KAWARA_OT_lay_ridge,
    KAWARA_OT_lay_hip,
    KAWARA_OT_lay_verge_line_left,
    KAWARA_OT_lay_verge_line_right,
    KAWARA_OT_cut_verge_line_overhang,
    KAWARA_OT_cut_all_overhang,
    KAWARA_OT_cut_flat_overhang,
    KAWARA_OT_cut_eave_overhang,
    KAWARA_OT_cut_hip_overhang,
    KAWARA_OT_cut_ridge_overhang,
    KAWARA_OT_delete_ridge_end_tile,
)


def _get_kawara_set_folder():
    """このアドオンファイルと同じ場所にある KawaraSet フォルダのパスを返す。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "KawaraSet")


def _find_layer_collection(layer_coll, target_name):
    """view_layerのレイヤーコレクション階層から、名前が一致するものを探す。"""
    if layer_coll.collection.name == target_name:
        return layer_coll
    for child in layer_coll.children:
        found = _find_layer_collection(child, target_name)
        if found is not None:
            return found
    return None


def _hide_and_exclude_collection(context, collection):
    """コレクションをビューポート非表示にし、ビューレイヤーからも除外する
    (参照(Link)自体は残したまま、画面上・評価対象から隠すだけ)。
    """
    collection.hide_viewport = True
    layer_coll = _find_layer_collection(context.view_layer.layer_collection, collection.name)
    if layer_coll is not None:
        layer_coll.exclude = True


def _load_kawara_set_file(context, fname):
    """指定した1つの .blend ファイルから、今のシーンにまだ無い瓦セット(コレクション)を
    Link で読み込む(ファイル本体はコピーせず参照だけにする)。
    読み込んだコレクションは、ビューポート非表示・ビューレイヤー除外にしておく
    (瓦セット自体は画面に表示する必要が無い「素材置き場」のため)。
    戻り値: 実際に読み込んだコレクション名のリスト。
    """
    scene = context.scene
    folder = _get_kawara_set_folder()
    path = os.path.join(folder, fname)
    if not os.path.isfile(path):
        return []

    scene_children_names = {c.name for c in scene.collection.children}
    loaded = []

    try:
        with bpy.data.libraries.load(path, link=True) as (data_from, data_to):
            to_load = [name for name in data_from.collections if name not in scene_children_names]
            data_to.collections = to_load

        for col in data_to.collections:
            if col is None:
                continue
            if col.name not in [c.name for c in scene.collection.children]:
                scene.collection.children.link(col)
                loaded.append(col.name)
    except Exception as e:
        print(f"KawaraTiles: 瓦セットの読み込みに失敗しました({fname}): {e}")

    context.view_layer.update()
    for name in loaded:
        col = bpy.data.collections.get(name)
        if col is not None:
            _hide_and_exclude_collection(context, col)

    return loaded


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Collection.kawara_spec = PointerProperty(type=KawaraPatternSpec)
    # Link(参照専用)で読み込んだコレクションはプロパティを編集できないため、
    # 実際に画面で編集・使用する寸法スペックはシーン側に持たせる(常にローカルで編集可能)。
    # コレクション側の kawara_spec は「初期値の参照元」としてだけ残す。
    bpy.types.Scene.kawara_working_spec = PointerProperty(type=KawaraPatternSpec)
    bpy.types.Scene.kawara_tool_props = PointerProperty(type=KawaraToolProps)


def unregister():
    del bpy.types.Scene.kawara_tool_props
    del bpy.types.Scene.kawara_working_spec
    del bpy.types.Collection.kawara_spec
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

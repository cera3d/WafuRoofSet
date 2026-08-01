if "roof_dxf" in locals():
	import importlib
	importlib.reload(roof_dxf)
else:
	from . import roof_dxf

if "kawara" in locals():
	import importlib
	importlib.reload(kawara)
else:
	from . import kawara

import bpy


class WAFU_OT_set_tab(bpy.types.Operator):
	bl_idname = "wafu.set_tab"
	bl_label = "タブ切り替え"
	tab: bpy.props.StringProperty()

	def execute(self, context):
		context.scene.wafu_roof_set_props.active_tab = self.tab
		return {'FINISHED'}


class WafuRoofSetProps(bpy.types.PropertyGroup):
	active_tab: bpy.props.EnumProperty(
		name="タブ",
		items=[
			('ROOF', "屋根", "屋根生成アドオンのパネルを表示"),
			('KAWARA', "瓦", "瓦配置アドオンのパネルを表示"),
		],
		default='ROOF',
	)


class WAFU_PT_main_panel(bpy.types.Panel):
	bl_label = "瓦屋根セット"
	bl_idname = "WAFU_PT_main_panel"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = "瓦屋根"

	def draw(self, context):
		layout = self.layout
		props = context.scene.wafu_roof_set_props

		row = layout.row(align=True)
		for item in props.bl_rna.properties['active_tab'].enum_items:
			op = row.operator("wafu.set_tab", text=item.name,
			                  depress=(props.active_tab == item.identifier))
			op.tab = item.identifier

		if props.active_tab == 'ROOF':
			roof_dxf.draw_roof_panel(layout, context)
		else:
			kawara.draw_kawara_panel(layout, context)


classes = (
	WAFU_OT_set_tab,
	WafuRoofSetProps,
	WAFU_PT_main_panel,
)


def register():
	roof_dxf.register()
	kawara.register()
	for cls in classes:
		bpy.utils.register_class(cls)
	bpy.types.Scene.wafu_roof_set_props = bpy.props.PointerProperty(type=WafuRoofSetProps)
	print("Registered 瓦屋根セット")


def unregister():
	del bpy.types.Scene.wafu_roof_set_props
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
	kawara.unregister()
	roof_dxf.unregister()
	print("Unregistered 瓦屋根セット")

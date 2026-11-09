/// <reference types="vite/client" />

/* vue-konva 全局组件类型声明：main.ts 中 app.use(VueKonva) 以默认前缀 "V"
   注册 Stage/Layer/Group/Rect/Image/Text/Line 等节点组件（模板中以
   <v-stage> 等小写使用），此处补齐 GlobalComponents 接口供 vue-tsc 推导。 */
import 'vue'
import type {
  Arc,
  Arrow,
  Circle,
  Ellipse,
  Group,
  Image as KonvaImage,
  Label,
  Layer,
  Line,
  Path,
  Rect,
  RegularPolygon,
  Ring,
  Shape,
  Sprite,
  Stage,
  Star,
  Tag,
  Text,
  TextPath,
  Transformer,
  Wedge,
} from 'vue-konva'

declare module 'vue' {
  export interface GlobalComponents {
    /** Konva 舞台根节点 */
    VStage: typeof Stage
    /** Konva 图层 */
    VLayer: typeof Layer
    /** Konva 节点分组 */
    VGroup: typeof Group
    /** Konva 矩形 */
    VRect: typeof Rect
    /** Konva 图片 */
    VImage: typeof KonvaImage
    /** Konva 文本 */
    VText: typeof Text
    /** Konva 线段 */
    VLine: typeof Line
    /** Konva 圆形 */
    VCircle: typeof Circle
    /** Konva 箭头 */
    VArrow: typeof Arrow
    /** Konva 弧形 */
    VArc: typeof Arc
    /** Konva 椭圆 */
    VEllipse: typeof Ellipse
    /** Konva 标签容器 */
    VLabel: typeof Label
    /** Konva 路径 */
    VPath: typeof Path
    /** Konva 正多边形 */
    VRegularPolygon: typeof RegularPolygon
    /** Konva 圆环 */
    VRing: typeof Ring
    /** Konva 自定义形状 */
    VShape: typeof Shape
    /** Konva 精灵动画 */
    VSprite: typeof Sprite
    /** Konva 星形 */
    VStar: typeof Star
    /** Konva 标签内文本块 */
    VTag: typeof Tag
    /** Konva 路径文本 */
    VTextPath: typeof TextPath
    /** Konva 变换器 */
    VTransformer: typeof Transformer
    /** Konva 扇形 */
    VWedge: typeof Wedge
  }
}

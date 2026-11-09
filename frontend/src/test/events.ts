/**
 * 文件级注释：单测事件派发工具。
 * jsdom 未实现 PointerEvent 构造器，且 @vue/test-utils 的 trigger 无法为只读的
 * clientX 赋值，因此这里用 MouseEvent 派发指针事件，并补上 pointerId。
 */

/** 指针事件类型（仅覆盖组件实际监听的事件） */
export type PointerEventType = 'pointerdown' | 'pointermove' | 'pointerup' | 'pointercancel'

/**
 * 派发一次指针事件。
 * @param el 目标元素
 * @param type 事件类型
 * @param clientX 指针横坐标（视口坐标系）
 * @param pointerId 指针唯一标识
 * @returns 无
 */
export function firePointer(el: Element, type: PointerEventType, clientX: number, pointerId = 1): void {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, button: 0 })
  Object.defineProperty(event, 'pointerId', { value: pointerId, configurable: true })
  el.dispatchEvent(event)
}

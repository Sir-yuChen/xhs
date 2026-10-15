/** 属性字段列表的**纯装配**（不依赖 React，可在 node 里直跑）。
 *
 * 用户口径（2026-10-15）：
 * > 「第三步，类目生成后，商品的其他属性中没有展示货号属性，
 * >   它**应该是固定的其它属性第一个**」
 *
 * ⚠️ 「货号」**不在平台下发的 `descProperties` 里**（离线取证
 * `step2_default_net.json` 真机 22 项逐名核对：无「货号」；平台
 * `predictProductProperty` 也从不预测它）—— 它是**前端自己合成的**
 * 固定首条。千帆 bundle（`8091.*.js`）逐字证据：
 *
 * ```js
 * ARTICLE_NO_PROPERTY_ID: function(){ return "articleNo" },
 * allProperties: function(e){
 *   return [{ propertyId: this.ARTICLE_NO_PROPERTY_ID, propertyName: "货号",
 *             propertyInputType: TEXT, propertyDataType: TEXT, propertyValueRule: E }]
 *          .concat(e.properties)                       // ⇒ 货号固定排第一
 * }
 * ```
 * 并且它**不是**普通属性项：`getFormData()` 里合成 `attributes[]` 时按
 * `propertyId === "articleNo"` **单独走**，值取顶层的 `articleNo`；
 * 值经 `selectedPropertyVals` **`unshift`** 进属性列表 ⇒ 提交体 `attributes[]`
 * 的**第一条**就是 `{attributeId:"articleNo", attributeValue:<用户填的货号>}`。
 *
 * 前端契约：与「商品属性」区其它项同形状（`PropertyField`），住进
 * 「其他属性」的**第 0 位**，`id` 与平台下发项天然不冲突（平台 id 是 24 位
 * hex；`articleNo` 与后端归一化后的属性 id 之间不会重名，仍走守卫去重）。
 */

/** 合成「货号」属性项用的常量（`id` 与千帆 bundle 的 `ARTICLE_NO_PROPERTY_ID` 逐字一致）。 */
export const ITEM_NO_PROPERTY_ID = "articleNo";
export const ITEM_NO_PROPERTY_NAME = "货号";
/** 平台对货号的输入上限（真机「货号 0/60」）。 */
export const ITEM_NO_MAX_LENGTH = 60;

/** 一件属性项的最小形状（`publish-config-step2.tsx` 的 `PropertyField` 是同构超集）。 */
export type PropertyLike = {
  id: string;
  name: string;
  kind: "select" | "multi" | "text" | "bool";
};

/** 合成「货号」属性项。
 *
 * ⚠️ 用**函数**而不是模块级常量：常量的对象会被多个调用点共享引用，
 * 任一处就地改动就污染全局（本项目的 `PropertyField` 是可变对象）。
 */
export function buildItemNoProperty(): {
  id: string;
  name: string;
  kind: "text";
  options: string[];
  values: { name: string; value_id: string }[];
  maxLength: number;
} {
  return {
    id: ITEM_NO_PROPERTY_ID,
    name: ITEM_NO_PROPERTY_NAME,
    //: 货号是**纯文本**属性（真机 `propertyInputType: TEXT`），候选值为空。
    kind: "text",
    options: [],
    values: [],
    maxLength: ITEM_NO_MAX_LENGTH,
  };
}

/** 「货号」是否已在该列表里（判据 = `id` **或** 名）。
 *
 * ⚠️ 判据必须是「id 或名」：平台某些类目的属性会被后端归一化成一个
 * 非 `articleNo` 的 24 位 id、而名字仍叫「货号」（离线数据里未见，
 * 但真机文档 `attributes[].attributeId` 用的是平台 id）⇒ 只判 id 会插出
 * **两条货号**，只判名会在平台改名时插出两条。
 *
 * ⚠️ **必须是泛型 `<T extends PropertyLike>`**（2026-10-15 补）：调用点传进来的是
 * 带 `kind` / `values` / `maxLength` 的**完整平台字段**，返回窄类型 `PropertyLike[]`
 * 会让下游 `f.values` 直接 `TS2339`。泛型保留入参类型，过滤器只负责「筛掉货号」。
 */
export function withoutItemNo<T extends PropertyLike>(fields: T[]): T[] {
  return fields.filter(
    (f) => f.id !== ITEM_NO_PROPERTY_ID && f.name.trim() !== ITEM_NO_PROPERTY_NAME,
  );
}

/** 把「货号」**固定在首位**，其余保持平台顺序（去重：平台若也下发了货号，不留两条）。
 *
 * 判据（用户验收）：
 *  · 列表**第一项恒为「货号」**，不论平台是否下发；
 *  · 平台下发了同名/同 id 项 ⇒ **不出现两条**（用平台那份、移到首位）。
 *
 * ⚠️ 返回类型写成 `PropertyLike[]`（不是泛型 `T[]`）：调用点拿到的是
 * 「货号项 + 平台项」的**同形状并集**，泛型会把「合成的货号项」硬塞成平台
 * 字段的具体类型 ⇒ `TS2322`（PropertyLike 不是 T 的子类型）。
 */
export function withItemNoFirst(fields: PropertyLike[]): PropertyLike[] {
  return [buildItemNoProperty(), ...withoutItemNo(fields)];
}

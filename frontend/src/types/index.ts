export type PlatformId = "xhs" | "douyin" | "kuaishou" | "weibo" | "xianyu" | "taobao";

export type PlatformMeta = {
  id: PlatformId;
  name_cn: string;
  name_en: string;
  enabled: boolean;
  status: "enabled" | "coming_soon";
  accent_color: string;
  icon: string;
};

export type Paginated<T> = {
  total: number;
  page: number;
  page_size: number;
  items: T[];
};

export type DashboardOverview = {
  platform: "xhs";
  today_crawls: number;
  saved_notes: number;
  pending_publishes: number;
  healthy_accounts: number;
  at_risk_accounts: number;
  comment_count?: number;
  total_engagement?: number;
  hot_topics: Array<{ keyword: string; notes: number; engagement: number }>;
  recent_activity: Array<{ type: string; title: string; status: string }>;
};

export type PlatformUser = {
  id: number;
  username: string;
};

export type AuthTokens = {
  access_token: string;
  refresh_token?: string;
  token_type: "bearer";
};

export type AuthPayload = AuthTokens & {
  user: PlatformUser;
};

/**
 * 一条凭据通道的登录态（设计 13 §18.5）。
 *
 * **三层判据各有各的答案，所以状态是五个互斥取值而不是一个布尔**：
 * - `missing`  —— 这个通道**从没导入过**（L1 没过）；
 * - `guest`    —— 导入了，但只是**游客态 / 空壳**（L2 没过：www 缺 `id_token`、
 *                 ark 缺 customer 单点登录键）；
 * - `unknown`  —— 存了、也是登录态，但**还没检测**（或上次检测没得到结论）；
 * - `valid` / `expired` —— 探活通过 / 平台明确拒绝（只有这两者是 L3 的结论）。
 *
 * ⚠️ **`unknown` 不是「已失效」**：用「没检查」冒充「不可用」会让用户去做一次
 * 根本不需要的重登（DEF-057 的同型红线）。
 */
export type XhsLoginStateValue = "missing" | "guest" | "valid" | "expired" | "unknown" | string;

export type XhsLoginChannelState = {
  state: XhsLoginStateValue;
  /** 后端给的原因全文（标签 tip 只用短映射，这里留作将来详情展示）。 */
  reason: string;
  /** 这一次真的打过只读探针吗（L3）。 */
  probed: boolean;
  /** 平台展示名（`www` → 「网页版（PC）」；`creator` → 「创作者平台」；`ark` → 「千帆」）。 */
  label: string;
};

export type XhsLoginChannels = Record<string, XhsLoginChannelState>;

export type XhsAccountLoginState = {
  platform_account_id: number;
  probe?: boolean;
  channels: XhsLoginChannels;
  checked_at?: string | null;
};

export type PlatformAccount = {
  id: number;
  platform: PlatformId;
  sub_type: "pc" | "creator" | "qianfan" | null;
  external_user_id?: string;
  nickname: string;
  avatar_url?: string;
  status: "active" | "healthy" | "expired" | "risk" | "unknown" | string;
  status_message?: string;
  profile?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  action?: "created" | "updated" | string;
  /**
   * 三个平台的登录态（L1/L2，**零平台请求**，随列表一起下发 —— 设计 13 §18.5）。
   *
   * 随列表给而不是逐卡片再问一次：既省 N 次往返，也让卡片上的登录态与账号资料
   * 来自**同一次响应**（不会出现「列表刷新了、登录态还是上一轮」）。
   * 老响应 / 别的调用点可能没有这个字段 —— 消费方按「没有就当没算过」处理。
   *
   * 2026-09-21 拆分（用户拍板）：千帆与创作者平台**不再是同一份令牌**，所以这里是
   * **三个**平台（`ark` 千帆 / `creator` 创作者 / `www` 网页版），不是两个。
   */
  login_channels?: XhsLoginChannels;
};

export type XhsQrLoginSession = {
  session_id: number;
  status: "pending" | "scanned" | "confirmed" | "expired" | string;
  qr_url: string;
  qr_image_data_url?: string;
  account?: PlatformAccount | null;
  creator_account?: PlatformAccount | null;
  /** Set when a PC login succeeded but the optional Creator sync did not. */
  creator_sync_error?: string | null;
  /**
   * 续期（`target_account_id`）成功时写进的那个平台：`www` / `creator` / `ark`。
   *
   * 回传它是为了**让界面说出令牌落在哪**（2026-09-21 拆分后三个平台各自独立，
   * 说清楚比让用户猜好）。
   */
  renewed_channel?: string;
  /**
   * 千帆续期写入的店铺名（仅千帆扫码有；其它恒为空）。
   *
   * 千帆的身份锚点是店铺而不是小红书用户，所以把店名回传，让界面能显示
   * 「已写入千帆令牌（店铺：观隅好物的店）」—— 用户立刻看出扫对没扫对。
   */
  renewed_shop?: { shop_user_id?: string; shop_name?: string } | null;
  /**
   * 续期失败的结构化原因（`{code, message}`）。**只有**扫到的身份与目标账号
   * 不一致、或目标账号不可用时才有 —— 此时**一个字节都没写**。
   */
  renew_error?: { code?: string; message?: string } | null;
  /**
   * 千帆登录**成功**（令牌已落库、账号已就位），但**店铺关系没绑上**的原因。
   *
   * 2026-09-26：登录成功即自动建店铺关系；只有「这个店已经绑在别的账号上」
   * 这类 1:1 冲突才会走到这里。非空时界面必须说出来 —— 否则「登录成功」
   * 会被用户读成「商品也能读了」，而我的笔记页仍会显示「尚未绑定店铺」。
   */
  shop_bind_error?: { code?: string; message?: string } | null;
};

export type NoteGoods = {
  goods_id: string;
  title: string;
  price: string;
  image: string;
  url: string;
  shop_name: string;
  /** 规格描述（千帆口径 `variant_desc`，如「喵宠开心/40*60CM」）。 */
  variant?: string;
};

/**
 * 千帆（店铺侧）为一条笔记给出的指标。
 *
 * 「创作者账户数据全部接入千帆」的落地：千帆商品笔记列表里的每个字段都透出到
 * 前端，而不是只取商品那一小块。**读不到的项是 `null`，不是 0** —— 缺值与真值
 * 必须能被区分（设计文档 §18.2 / §19）。
 */
export type XhsNoteShopMetrics = {
  note_id: string;
  title: string;
  cover: string;
  note_flag: string;
  browse_num: number | null;
  browse_num_30d: number | null;
  likes: number | null;
  collects: number | null;
  comments: number | null;
  shares: number | null;
  pay_gmv: number | null;
  pay_order_cnt: number | null;
  status: number | null;
  note_type: number | null;
  publish_time: number | null;
  xsec_token: string;
};

/** 单篇笔记详情里，千帆给出的挂载商品（v1.18：三源合并）。 */
export type XhsNoteShopGood = {
  item_id: string;
  item_title: string;
  item_image: string;
  variant_desc: string;
  price: string;
};

/** `XhsSearchNote.shop` 在详情弹窗里的完整形状：指标平铺 + 商品数组。 */
export type XhsNoteShopDetail = XhsNoteShopMetrics & {
  /** 是否绑定了店铺（决定「千帆数据」这一区块显示与否） */
  bound: boolean;
  /** 该笔记在千帆确实有数据（有映射行） */
  available: boolean;
  /** 店铺名 */
  source: string;
  /** 失败原因（不阻断详情展示，只做一行提示） */
  error: string;
  goods: XhsNoteShopGood[];
};

export type XhsSearchNote = {
  note_id: string;
  /** 千帆（店铺侧）数据；列表页只有指标，详情弹窗里还带商品数组（v1.18）。 */
  shop?: XhsNoteShopDetail | null;
  note_url?: string;
  title: string;
  content: string;
  author_id: string;
  author_name: string;
  author_avatar: string;
  cover_url: string;
  likes: number;
  collects: number;
  comments: number;
  shares: number;
  type: string;
  timestamp?: number | string;
  image_urls?: string[];
  video_url?: string;
  video_addr?: string;
  tags?: string[];
  /** null means "not parsed" — the backend never fabricates a placeholder. */
  goods?: NoteGoods | null;
  raw: Record<string, unknown>;
};

export type XhsNoteSearchResponse = {
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  items: XhsSearchNote[];
  raw: Record<string, unknown>;
};

/**
 * Which platform link served the data.
 *
 * ``pc``      — www (edith) endpoints; requires a ``web_session`` cookie.
 * ``creator`` — creator platform (galaxy) endpoints; the note manager.
 * ``""``      — neither is usable for the requested scope.
 */
export type XhsSelfLinkKind = "pc" | "creator" | "";

/** Platform identity of the signed-in account (from POST /xhs/pc/users/me). */
export type XhsSelfIdentity = {
  user_id: string;
  nickname: string;
  avatar: string;
  red_id: string;
  ip_location: string;
  link?: XhsSelfLinkKind;
  raw?: Record<string, unknown>;
};

/** One self-content link's availability, always with a human-readable reason. */
export type XhsSelfCapability = {
  available: boolean;
  /** Non-empty whenever `available` is false — never leave the user guessing. */
  reason: string;
  /** 仅用户维度三分类带：这次读数是靠自有会话还是借同身份兄弟账号的（v1.20）。 */
  session_source?: XhsSelfSessionSource;
};

/**
 * 用户维度三分类（我赞过的 / 我收藏的 / 互动消息）的会话来源。
 *
 * 只有 `own` 一种取值：账号自己没有 `web_session` 时后端直接给结构化原因。
 */
export type XhsSelfSessionSource = "own" | "";

/**
 * Capability report from POST /xhs/pc/users/capabilities.
 *
 * Computed locally on the server (no platform request), so the page can rely on
 * it even when identity resolution fails. This is the single source of truth for
 * which tabs are usable — never infer it from `sub_type` on the client.
 */
export type XhsSelfCapabilities = {
  account_id: number;
  sub_type: string;
  has_web_session: boolean;
  /** 用户维度三分类实际用的会话来源；`""` = 都没有（这三类置灰）。 */
  session_source: XhsSelfSessionSource;
  notes_link: XhsSelfLinkKind;
  capabilities: Record<XhsSelfContentScope | "interactions" | "goods", XhsSelfCapability> & {
    goods: XhsSelfGoodsCapability;
  };
};

/**
 * 商品能力声明：**来源 + 读取条件**，不是一句「暂不支持」。
 *
 * 笔记挂载的商品在小红书的网页与创作者接口里都不存在（设计文档 §19），
 * 唯一真实来源是千帆商家后台（`source: "ark"`）。所以这条能力回答的是
 * 「有没有绑定店铺、同步过多少条」，前端据此决定是展示绑定入口还是同步按钮。
 */
export type XhsSelfGoodsCapability = XhsSelfCapability & XhsGoodsSource;

/** `POST /xhs/pc/users/notes` 附带的商品来源元信息（与 `capabilities.goods` **同源同形**）。 */
export type XhsGoodsSource = {
  source: string;
  bound: boolean;
  //: 2026-09-22 移除 `shop_account_count`：用户口径「店铺 ↔ 账号 1:1，不允许同店多账号」，
  //: 「同一店铺被几个账号绑着」不再是一个事实（恒 0/1）。后端字段已删，前端不再读。
  shop_name: string;
  status: string;
  status_message: string;
  shop_total: number;
  synced_notes: number;
  synced_at: string;
  /** 最近一次千帆登录态巡检时间（T-31）。 */
  health_checked_at?: string;
  /** 按需补齐（T-30）本次的结果：请求了几条 / 补到几条 / 失败原因。 */
  on_demand?: { requested: number; stored: number; error: string };
};

/** `POST /xhs/qianfan/binding` 的响应（绑定状态）。 */
export type XhsQianfanBinding = {
  platform_account_id: number;
  bound: boolean;
  /** 店铺实体主键（`shops.id`）。2026-09-22 起账号 ↔ 店铺是 **1:1**。 */
  shop_id_fk?: number | null;
  shop_user_id: string;
  shop_name: string;
  status: string;
  status_message: string;
  goods_note_total: number;
  synced_notes: number;
  goods_synced_at: string;
  /** 最近一次千帆登录态巡检时间（T-31）；空 = 从未巡检。 */
  health_checked_at?: string;
};

/** 千帆商品列表的状态筛选键（与后端 `QIANFAN_ITEM_STATUSES` 一一对应）。 */
export type XhsShopItemStatus =
  | "all"
  | "on_shelf"
  | "off_shelf"
  | "frozen"
  | "auditing"
  | "audit_rejected"
  | "audit_passed"
  | "rectify";

/**
 * `POST /xhs/qianfan/items` 的一个商品。
 *
 * 金额字段一律是**分**（千帆口径，原样透传），展示层自己除以 100 ——
 * 这样「有没有乘错过 100」在契约上就是不可能发生的事。
 */
export type XhsShopItem = {
  item_id: string;
  title: string;
  article_no: string;
  image: string;
  images: string[];
  image_count: number;
  price_min_fen: number | null;
  price_max_fen: number | null;
  stock: number | null;
  sku_count: number | null;
  on_sale_sku_count: number | null;
  sales_total: number | null;
  sales_30d: number | null;
  /**
   * 挂链笔记篇数（**关系表口径**：`note_goods` 按商品计数，设计 13 §32.4）。
   * 本地生成一篇挂该商品的笔记就会 +1 —— 这是列表列显示的数字。
   */
  note_count: number;
  /**
   * 平台口径的挂链笔记数（原文 `product_note_num`）—— **只作对照**。
   * 本地生成的笔记在平台侧还不存在 ⇒ 这个数不会动；关系表只覆盖
   * 「已同步 + 本地生成」⇒ 本地 ≤ 平台是常态（Tooltip 里两个都说明）。
   */
  platform_note_count: number;
  note_ids: string[];
  category: string;
  status_label: string;
  buyable: boolean;
  frozen: boolean;
  deleted: boolean;
  audit_status: number | null;
  check_status: number | null;
  is_channel: boolean;
  stock_warning_sku_count: number;
  create_time: string;
  on_shelf_time: string;
  update_time: string;
  xsec_token: string;
};

export type XhsShopItemsQuery = {
  platform_account_id: number;
  page_no?: number;
  page_size?: number;
  keyword?: string;
  status?: XhsShopItemStatus;
  /** 元（用户口径）；后端转分后发给千帆。 */
  price_from?: number;
  price_to?: number;
  stock_from?: number;
  stock_to?: number;
  sales_from?: number;
  sales_to?: number;
  /** 创建时间（`YYYY-MM-DD`；结束日含当天）。后端转成千帆要的 Unix 秒。 */
  create_time_from?: string;
  create_time_to?: string;
  /** 挂链笔记数的比较符（v1.34）：`lt`/`lte`/`eq`/`gte`/`gt`，**默认 `lt`（小于）**。 */
  note_count_op?: XhsNoteCountOp;
  /**
   * 挂链笔记数的比较值（v1.34）。**不传 ⇒ 不筛**（不是「小于 0」）；
   * 口径 = `note_goods` **关系表**，与列表「挂链笔记」列是同一个数（设计 13 §33.8）。
   */
  note_count?: number;
  sort_field?: string;
  order?: "asc" | "desc";
};

/** 挂链笔记数筛选的比较符（v1.34）；白名单与后端 `qianfan.NOTE_COUNT_OPS` 逐个相等。 */
export type XhsNoteCountOp = "lt" | "lte" | "eq" | "gte" | "gt";

/**
 * `POST /xhs/qianfan/item-notes` 的一条挂链笔记（**关系表源**，设计 13 §31.6）。
 *
 * v1.33 起数据源从 `shop_note_goods` 平台证据缓存切换到 `note_goods` join `notes`
 * （本地实体关系）—— 弹窗要展示的是「本地这条笔记」的正文 / 图集 / 发布态，
 * 缓存表没有这些。互动指标只有同步链路落库的笔记带（`raw_json` 里存了平台条目），
 * 本地生成的笔记这些字段是 `null`（缺值口径：不造假）。
 */
export type XhsItemNote = {
  note_id: string;
  title: string;
  /** 正文（关系表源新增；本地生成笔记的内容）。 */
  content: string;
  cover: string;
  /** 图集（`note_assets` 的图片 URL，按 sort_order）。 */
  images: string[];
  /** 视频地址（有则视频笔记优先展示）。 */
  video_url: string;
  publish_time: string;
  browse_num: number | null;
  like_num: number | null;
  collect_num: number | null;
  comment_num: number | null;
  share_num: number | null;
  note_url: string;
  /** `synced` / `local` / `local_generated`（设计 13 §31：本地生成待发布笔记）。 */
  note_type: string;
  /** 发布状态机（仅 `local_generated` 有意义）：`""` / `pending` / `publishing` / `published` / `failed`。 */
  publish_status: string;
  /** 发布失败原因（平台原文；空 = 没失败过）。 */
  publish_error: string;
  synced_at: string;
};

export type XhsItemNotesResponse = {
  item_id: string;
  count: number;
  notes: XhsItemNote[];
  /** 本地已映射的笔记数 / 店铺商品笔记总数 —— 用来诚实说明「可能不全」。 */
  synced_notes: number;
  goods_note_total: number;
  goods_synced_at: string;
};

export type XhsShopItemsResponse = {
  platform_account_id: number;
  /** v1.32 起恒为 `local`（本地台账；改造前是 `ark` 实时查询）。 */
  source: string;
  shop_name: string;
  status: XhsShopItemStatus;
  status_label: string;
  page_no: number;
  page_size: number;
  total: number;
  count: number;
  has_more: boolean;
  /**
   * **实际生效的排序口径**（设计 13 §32.1）：后端是排序的唯一权威 —— 前端表头
   * 显示的就是这两个值（未知字段会在后端回退成 `create_time` / `desc`）。
   */
  sort_field: string;
  order: string;
  /** **本地同步时间**（本店 max(synced_at)，不是查询时间；空 = 从没同步过）。 */
  queried_at: string;
  /** 平台商品总数（同步时回报；0 = 没同步过，**不是**「平台 0 件」）。 */
  products_total: number;
  /** 台账同步时间（`YYYY-MM-DD HH:MM`；空 = 没同步过）。 */
  products_synced_at: string;
  items: XhsShopItem[];
};

/**
 * `POST /xhs/qianfan/items/delete` 的响应（v1.36，批量删除；**只删本地台账行**）。
 *
 * `deleted` 与 `requested` **分开回**：后端回的是 `rowcount`（真实删掉的行数），
 * 不是「请求了几条就当删了几条」—— 传进来不存在的、或别的店的 item_id 时
 * `deleted < requested`，页面据此如实说明，不假装全删成功。
 */
export type XhsShopItemsDeleteResult = {
  deleted: number;
  /** 去重、去空白后的**有效**条数（后端校验过的口径）。 */
  requested: number;
  item_ids: string[];
};

/**
 * `POST /xhs/qianfan/item-detail` 的响应（v1.32，商品详情弹窗数据源；**纯本地**）。
 *
 * `detail_synced_at` 为空串 = 还没补过 SKU 详情（同步商品任务多轮补足）——
 * 此时 `skus` 为 `[]`，页面渲染空态 + 说明，**不渲染成 0 行**（缺值口径）。
 */
export type XhsShopItemSku = {
  sku_id: string;
  sku_name: string;
  variant_name: string;
  barcode: string;
  erp_code: string;
  /** 分（展示层 /100）。 */
  price_fen: number | null;
  original_price_fen: number | null;
  stock: number | null;
  spec_image: string;
  weight: number | null;
  buyable: boolean | null;
  freeze: boolean | null;
};

export type XhsShopItemDetail = {
  platform_account_id: number;
  item_id: string;
  shop_name: string;
  /** 与列表 `items[]` 元素同构的基本信息。 */
  item: XhsShopItem;
  /** 商品图集（主图 + 详情图合并去重；详情弹窗展示用）。 */
  images: string[];
  /**
   * 商品**主图**（`raw_json.images`）—— 生成详情图时可选参考图分组 ①（v2.15）。
   */
  product_images: string[];
  /**
   * **原商品详情图**（`raw_json.image_descriptions`，竖长图）—— 参考图分组 ②（v2.15 新增）。
   *
   * ⚠️ 与 `product_images` 是**两套不同的图**（真机实测：主图 10 张方图、
   * 详情图 5 张竖长图），不是同一组的子集。
   */
  detail_images: string[];
  skus: XhsShopItemSku[];
  /** SKU 数（详情补齐口径；没补过 = null）。 */
  sku_count: number | null;
  detail_synced_at: string;
  synced_at: string;
  /** 对账标记（设计 13 §27）：非空 = 最近一次同步发现「本地有、平台无」。 */
  platform_missing_at: string;
};

/** 商品详情图生成回执（`POST /xhs/qianfan/generate-detail-image`，设计 17 §13.3.3）。 */
export type GenerateDetailImageResult = {
  /** 服务商回传的出图地址（为空 = 服务商没回 URL，看 `asset.local_path`）。 */
  url: string;
  /** AIGC 溯源资产行（发布时按它取 `aigcTaskId`）。 */
  asset: {
    id: number;
    /** 本地落盘路径；空串 = 服务商没回本地路径（**不拿 url 冒充**）。 */
    local_path: string;
    url: string | null;
    prompt: string;
    model_name: string;
    /**
     * v2.20：这张图属于哪组参考图（`product` = 商品主图 / `detail` = 原商品详情图）。
     * ⚠️ 分组是**请求时就定的事实**（端点按组各发各的请求），不是事后按顺序猜的。
     */
    group?: "product" | "detail";
    /** 组中文名（后端下发，前端不在本地维护映射表）。 */
    group_label?: string;
    /** 组内第几张（1 起）。 */
    group_index?: number;
  };
  /**
   * v2.18：**本次生成的全部图**（张数由请求的 `count` 决定）。
   * `asset` / `url` 保留 = 首张（向后兼容单张时代的调用方）。
   */
  assets?: GenerateDetailImageResult["asset"][];
  /**
   * v2.20：**按组的结果**（主图 / 详情图两大块）—— 前端直接按它分块渲染，
   * 不在本地再 groupBy 一遍（分组口径由后端唯一裁定，DEF-044）。
   */
  groups?: {
    group: "product" | "detail";
    group_label: string;
    /** 该组本次要了几张。 */
    requested: number;
    /** 该组实际成了几张（失败要从这里一眼看出）。 */
    succeeded: number;
    assets: GenerateDetailImageResult["asset"][];
  }[];
  /** v2.18：逐张失败原因（半成功时才非空；全部失败 ⇒ 端点直接 502，不走这里）。 */
  errors?: string[];
};

export type XhsSelfContentScope = "notes" | "liked" | "collected";

export type XhsSelfContentResponse = {
  items: XhsSearchNote[];
  /** Opaque platform cursor; pass back verbatim to load the next page. */
  cursor: string;
  has_more: boolean;
  total: number;
  scope: XhsSelfContentScope;
  user_id: string;
  /** "url" / "auto" on the PC link; "creator" when the creator link served it. */
  token_source: string;
  identity: XhsSelfIdentity | null;
  /** 商品来源与覆盖率（仅 `scope === "notes"` 时返回）。 */
  goods_source?: XhsGoodsSource;
};

export type XhsInteractionKind = "like_collect" | "mention" | "connection";

export type XhsInteractionMessage = {
  kind: XhsInteractionKind | string;
  message_id: string;
  title: string;
  content: string;
  user_name: string;
  user_avatar: string;
  note_id: string;
  note_url: string;
  cover_url: string;
  time: number | string;
  raw: Record<string, unknown>;
};

export type XhsInteractionResponse = {
  items: XhsInteractionMessage[];
  cursor: string;
  has_more: boolean;
  total: number;
  kind: XhsInteractionKind | string;
};

/** 三分类各自的扫描元数据：结果是否完整、有没有失败、还能不能继续搜。 */
export type XhsSelfSearchScopeMeta = {
  label: string;
  scanned_pages: number;
  scanned_items: number;
  matched: number;
  /** true = 该分类已到最后一页（列表耗尽），不会再有更早的命中。 */
  exhausted: boolean;
  /** 非空表示该分类扫描失败；其余分类的结果仍然有效。 */
  error: string;
};

export type XhsSelfSearchItem = XhsSearchNote & { scope: XhsSelfContentScope };

export type XhsSelfSearchResponse = {
  keyword: string;
  items: XhsSelfSearchItem[];
  total_matched: number;
  /** true = 还有分类没扫到底，「继续搜索更早内容」才有意义。 */
  has_more: boolean;
  /** 原样回传给下一次请求即可续搜。 */
  cursors: Partial<Record<XhsSelfContentScope, string>>;
  scopes: Partial<Record<XhsSelfContentScope, XhsSelfSearchScopeMeta>>;
  user_id: string;
  token_source: string;
  identity: XhsSelfIdentity | null;
};

export type XhsSearchOptions = {
  account_id: number;
  keyword: string;
  page?: number;
  sort_type_choice?: number;
  note_type?: number;
  note_time?: number;
  note_range?: number;
  pos_distance?: number;
  geo?: string;
};

export type XhsDataCrawlMode = "note_urls" | "search" | "comments";

export type XhsDataCrawlPayload = {
  account_id: number;
  mode: XhsDataCrawlMode;
  urls?: string[];
  keyword?: string;
  pages?: number;
  max_notes?: number;
  time_sleep?: number;
  fetch_comments?: boolean;
  sort_type_choice?: number;
  note_type?: number;
  note_time?: number;
  note_range?: number;
  pos_distance?: number;
  geo?: string;
};

export type XhsDataCrawlItem = {
  source: string;
  status: "success" | "failed" | string;
  error: string;
  note?: XhsSearchNote | null;
  comments: NoteComment[];
  comment_count: number;
};

export type XhsDataCrawlResponse = {
  task: TaskRecord;
  total: number;
  success_count: number;
  failed_count: number;
  items: XhsDataCrawlItem[];
};

export type SavedNote = {
  id: number;
  platform: PlatformId;
  platform_account_id: number;
  /**
   * **来源账号昵称**（设计 13 §19.13 / 设计 04 §2.12）。
   *
   * 「已发布」本身就是账号维度的事实（哪个账号发过），界面不给账号就无从核对。
   * 昵称取不到时是空串 —— 前端按缺值口径**不展示**，不写「未知」充数。
   */
  account_name: string;
  note_id: string;
  title: string;
  content: string;
  author_name: string;
  /**
   * **是否已发布过**（设计 03 §4.1 / 设计 13 §19.12）。
   *
   * 由落库链路决定：笔记同步（www / creator / ark 三源）落库 ⇒ `true`；
   * 采集（发现页保存、监控采集）⇒ `false`。
   *
   * ⚠️ 2026-09-23 起它是 `publish_status` 的**投影**（设计 13 §32.3.2）：
   * 界面**不再单独渲染**它（卡片上只展示一个发布状态，由 `publish_status` 决定），
   * 它留给导出与既有消费者。
   */
  published: boolean;
  /**
   * **笔记类型**（设计 13 §25，2026-09-22）：这条数据是谁搬来的。
   *
   * - `synced` = 三方同步笔记（**同步链路**落库：账号卡片手动同步与定时任务同一条路）；
   * - `local` = 本地笔记（采集 / 监控 / 批量保存落库）；
   * - `local_generated` = **本地生成**（商品笔记智能生成链路落库，设计 13 §31）——
   *   待发布 / 发布失败 / 已发布三态由 `publish_status` 表达。
   *
   * 由**落库链路**决定、只升不降（与 `published` 同一把尺子），用户不可改。
   * 内容库详情卡的「同步」按钮**只对 `synced` 渲染** —— 本地笔记是采集来的别人
   * 的笔记，「拉平台最新内容」对它没有意义；「发布」按钮**只对 `local_generated`**
   * 且 `publish_status ∈ {pending, failed}` 渲染。
   */
  note_type: "synced" | "local" | "local_generated";
  /**
   * **发布状态**（设计 13 §32.3，2026-09-23 起**所有笔记都有值**、只有一个字段）。
   *
   * `pending` 待发布 / `publishing` 发布中（链路瞬时态）/ `published` 已发布 /
   * `failed` 发布失败（可重发，失败原因在 `publish_error`）。
   *
   * 卡片与详情**只渲染这一个状态**；`local_generated` 的状态由发布链路决定
   * （不接受手改，手改会被 400 `NOTE_STATUS_MANAGED` 挡回），其余笔记可在界面上
   * 手动切换「待发布 / 已发布」。
   */
  publish_status?: string;
  /** 最近一次发布失败原因（平台原文；成功即清空）。 */
  publish_error?: string;
  /** 发布尝试次数（含失败）。 */
  publish_attempts?: number;
  /** 三方发布成功时间（本地 ISO；空 = 没发布过）。 */
  published_at?: string | null;
  /**
   * **话题**（设计 13 §34，2026-09-25）：走 `note_topics` 关系表 join `topics`
   * 字典表查出的当下真值（原 `tags` 标签表已并入话题表；`notes.topics_json` 列已删）。
   * 空 = 这条笔记没有话题（不是错误）。
   */
  topics?: NoteTopic[];
  /**
   * **挂载商品**（设计 13 §28.3）：走 `note_goods` 关系表 join `shop_products`
   * 查出来当下的真值 —— 商品信息只在商品表存一份，笔记表不存快照。
   * 空 = 没挂商品（不是错误）。
   */
  goods?: Array<{
    item_id: string;
    item_title: string;
    /** 价格，**单位是分**的字符串（展示层换算成元）。 */
    price: string;
    images: string[];
    stock: number | null;
    sales: number | null;
  }>;
  raw_json?: Record<string, unknown>;
  asset_urls?: string[];
  cover_url?: string;
  video_url?: string;
  video_addr?: string;
  created_at: string;
  /**
   * 对账标记（设计 13 §24，2026-09-22）：平台列表里已经没有这条笔记的时间。
   * `null` = 正常；非空 = 最近一次对账发现「本地有、平台无」。
   *
   * **不删行**（用户口径：「本地多得数据不直接删除，打上状态」）—— 平台列表
   * 可能因筛选 / 分页不完整，删不可逆。标记是自愈的：平台又返回了就清回 null。
   */
  platform_missing_at?: string | null;
  /**
   * ⚠️ ~~按需补齐的结果~~ **字段已移除**（2026-09-22）：详情端点不再打平台
   * （用户口径「内容库应查询的均是本地数据」），`hydrate=true` 入口随之删除。
   *
   * 保留说明：它曾在 `GET /notes/{id}?hydrate=true` 时返回，让前端按失败三分法
   * 区分「取不到」与「真没数据」。那条路径的 `db.commit()` 会与同步任务抢
   * SQLite 写锁 ⇒ `database is locked` ⇒ 未捕获 ⇒ 用户看到「详情 500」。
   * 正文与图集的补齐现在只由同步链路做（有界 + 跨轮累积，设计 13 §19.13）。
   */
  /**
   * ⚠️ ~~`tags`（用户标签）~~ **字段已移除**（2026-09-25，设计 13 §34.4）：
   * 标签表已整表并入话题表 ⇒ 标签的展示 / 增删统一走 `topics`（关系表 join
   * 话题字典）。`tags` 留着就是同一语义两个字段（DEF-044 同款）。
   */
};

/**
 * `GET /notes` 的响应：分页 + **全库口径**的发布状态统计（设计 05 §2.5）。
 *
 * ⚠️ `stats` **不随筛选变化**（只按 `user_id + platform` 统计），所以它**不能**
 * 用 `items.length` 反推 —— 那会把「本页条数」当成「全库条数」。
 */
export type SavedNotesPage = Paginated<SavedNote> & {
  /**
   * 内容库**全库口径**的三档发布状态计数（不随筛选变化，设计 05 §2.5 / 设计 13 §32.3.3）。
   * 三数之和 = 该用户的笔记总数。
   */
  stats: { pending: number; published: number; failed: number };
};

export type AnalyticsTopContent = {
  id: number;
  note_id: string;
  title: string;
  author_name: string;
  created_at: string;
  likes: number;
  collects: number;
  comments: number;
  shares: number;
  engagement: number;
};

export type AnalyticsHotTopic = {
  keyword: string;
  notes: number;
  engagement: number;
};

export type AnalyticsCommentInsight = {
  total_comments: number;
  question_count: number;
  top_terms: Array<{ term: string; count: number }>;
  top_comments: Array<{
    id: number;
    note_id: number;
    user_name: string;
    content: string;
    like_count: number;
  }>;
};

export type BenchmarkTopNote = AnalyticsTopContent;

export type NoteAsset = {
  id: number;
  note_id: number;
  asset_type: "image" | "video" | string;
  url: string;
  local_path: string;
  download_url?: string;
  sort_order?: number;
};

export type NoteComment = {
  id: number;
  note_id: number;
  comment_id: string;
  user_name: string;
  user_id?: string | null;
  content: string;
  like_count: number;
  parent_comment_id?: string | null;
  created_at_remote?: string | null;
  raw_json?: Record<string, unknown>;
};

/**
 * **话题字典行**（设计 13 §34.4，2026-09-25）：原 `Tag`（用户标签）—— 标签表
 * 已整表并入话题表（`tags` / `note_tags` ⇒ `topics` / `note_topics`），
 * 类型随之改名。`color` 自标签表沿用（迁移时原样保留旧值）。
 */
export type NoteTopic = {
  id: number;
  name: string;
  color: string;
};

export type BatchTagNotesResponse = {
  updated_count: number;
  items: SavedNote[];
};

export type BatchCreateDraftsPayload = {
  note_ids: number[];
  intent?: "rewrite" | "publish" | string;
};

export type BatchCreateDraftsResponse = {
  created_count: number;
  items: Draft[];
};

export type NotesExportPayload = {
  note_ids: number[];
  format?: "json" | "csv";
};

export type NotesExportResponse = {
  exported_count: number;
  file_name: string;
  file_path: string;
  download_url: string;
};

export type AnalyticsReportPayload = {
  note_ids?: number[];
  format?: "json";
};

export type AnalyticsReportResponse = {
  report_type: "operations";
  generated_at: string;
  note_count: number;
  file_name: string;
  file_path: string;
  download_url: string;
  summary: {
    note_count: number;
    total_engagement: number;
    comment_count: number;
    top_topics: AnalyticsHotTopic[];
    top_notes: AnalyticsTopContent[];
    benchmark_count: number;
  };
};

export type SaveNotesResponse = {
  saved_count: number;
  items: SavedNote[];
};

export type Draft = {
  id: number;
  platform: PlatformId;
  title: string;
  body: string;
  tags?: { id?: string; name: string }[];
  source_note_id?: number | null;
  created_at: string;
};

export type CreateDraftPayload = {
  platform: "xhs";
  source_note_id?: number;
  title?: string;
  body?: string;
  intent?: "rewrite" | "publish" | string;
};

/**
 * 模型能力类型（v2.9 起四值，设计 17 §13.2.11）。
 *
 * - `text` 文本：只出文本（chat / reasoning）
 * - `image` 图片：只出图片（文生图 / 图生图）
 * - `multimodal` 多态：文本与图片都能出，**同时满足** text / image 两种业务点
 * - `video` 视频：出视频
 *
 * 归属真相源在后端 `business_provider_keys.MODEL_TYPE_LABELS`（前端零硬编码，
 * 选项由 `GET /llm/business-provider-keys` 的 `model_types` 下发）。
 */
export type ModelType = "text" | "image" | "multimodal" | "video";

// —— 「平台 × 业务点 → 服务商」映射（设计 17 §13.2，v2.0）——
//
// ⚠️ 一期 `ModelConfig`（本地模型配置表）已随 v2.0 退场：服务商是**单独配置**的
// （`LlmGatewayConfig`），业务通过**固定 key** 映射到服务商。同 DEF-044：退场语义
// 同批移除，不留影子类型。
export type BusinessProviderKind = ModelType;

/** 业务点定义（`GET /llm/business-provider-keys`，**后端下发**，前端零硬编码） */
export type BusinessKeyOption = {
  key: string;
  kind: BusinessProviderKind;
  label: string;
  /**
   * 触发源：该业务点在界面上被哪些入口触发（页面路径链，如
   * `商品列表-商品详情-生成商品详情图`）。空数组 = 只由 API 直接调用、界面无入口。
   */
  triggers: string[];
};

/** 业务点 → 服务商 映射行（`GET /llm/business-providers`） */
export type BusinessProviderRow = {
  platform: string;
  business_key: string;
  kind: BusinessProviderKind;
  /** 业务点中文名（常量元数据下发，只读展示） */
  label: string;
  /** 触发源：该业务点在界面上由哪些业务动作触发（只读展示；空 = 仅 API 调用） */
  triggers: string[];
  /** 空串 = 未配置 */
  provider_key: string;
  provider_name: string | null;
  provider_enabled: boolean | null;
  provider_model_type: BusinessProviderKind | null;
  /**
   * v2.17：**兼容性判据由后端唯一裁定**（`_type_satisfies`）—— 多态模型
   * （`multimodal`）可服务 text / image 两类业务点。
   *
   * ⚠️ 前端**不许**自己写 `provider_model_type === kind` 比较：那是判据的第二份实现，
   * 必然与后端漂移（2026-10-03 实测：多模态服务商在模型配置页被误报「类型不匹配」，
   * 而后端写入校验明明允许）。判据唯一源在后端，这里只读它渲染。
   */
  type_satisfied?: boolean;
  /**
   * v2.3：已选**具体模型**（服务商下的某个模型）。空串 = 只选了服务商、还没选模型。
   * ⚠️ 服务商 + 模型**两者都必选**，缺一调用即失败（不回落任何服务商级默认模型，v2.8 起连该列都已删）。
   */
  model_name: string;
  /** v2.3：该服务商的模型目录（前端模型下拉候选，由 `models_json` 解析） */
  provider_models: string[];
  /**
   * v2.7：该服务商的**模型行**（带 `model_type` / `platform`）。
   *
   * ⚠️ 与 `provider_models` 的分工（§13.2.9.4）：
   * - `provider_models` = 上游 `/models` 拉到的**候选池**（字符串，可能含未配置的）
   * - `provider_model_rows` = 用户**已配置**的模型行（带类型，能真正参与调用）
   *
   * 「模型下拉按业务点 kind 过滤」读**本字段** —— 候选池没有类型信息，
   * 且未配置的候选在 v2.7 起无法调用（后端 400 `LLM_BUSINESS_MODEL_NOT_CONFIGURED`）。
   * 旧代码（v2.0）读的是服务商级 `provider_model_type`，该字段已随服务商级类型退场。
   */
  provider_model_rows: LlmGatewayModelRow[];
  /** v2.3：**服务商与模型都选了**才算配置好（只选服务商 ⇒ false） */
  configured: boolean;
  enabled: boolean;
};

export type BusinessProviderListResponse = {
  items: BusinessProviderRow[];
  platforms: { value: string; label: string }[];
};

export type BusinessKeysResponse = {
  items: BusinessKeyOption[];
  model_types: { value: BusinessProviderKind; label: string }[];
};

export type BusinessProviderUpsertPayload = {
  platform: string;
  business_key: string;
  /** 空串 = 清空映射（回到「未配置」） */
  provider_key: string;
  /**
   * v2.3：具体模型。`provider_key` 非空时**必填**（空 ⇒ 400 `LLM_BUSINESS_MODEL_REQUIRED`），
   * 且必须在服务商模型目录内（否则 400 `LLM_BUSINESS_MODEL_NOT_IN_CATALOG`）。
   * `provider_key` 为空时后端会一并清空。
   */
  model_name?: string;
};


export type RewriteDraftPayload = {
  draft_id: number;
  instruction?: string;
};

// —— LLM 网关（设计 17 §7.1，M1/T1；v1.7 任意 OpenAI 兼容上游连接配置）——
export type LlmGatewayConfig = {
  id: number;
  name: string;
  /** 服务商唯一 KEY（v1.9）：系统生成、创建后不可编辑；业务代码按它引用服务商 */
  provider_key: string;
  base_url: string;
  /**
   * ⚠️ **v2.7/v2.8 出参变更**：
   * `platform` / `model_type` 已下沉到**模型行**（本数组每项的字段），
   * `default_model` 整列删除（§13.2.10）—— 三者都不再是服务商级字段。
   * 能力类型（text/image）与使用平台（xhs/douyin/…）请读 `models[*]`。
   */
  models: LlmGatewayModelRow[];
  /** v2.7：上游 `/models` 拉到的候选池（字符串数组，供下拉；未配置的模型不在此列） */
  catalog: string[];
  has_access_token: boolean;
  enabled: boolean;
  healthy: boolean;
  last_health_at: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /**
   * v2.9：**仅创建响应**带此字段** —— 新建服务商时后端自动同步一次模型
   * （拉目录 + AI 归纳类型）。前端据 `ok` 决定提示文案；**失败不阻断创建**，
   * 服务商已建成，用户可在操作列点「同步服务商模型」重试。
   * `GET` 列表 / `PATCH` 的响应都没有这个字段。
   */
  sync?: LlmGatewaySyncSummary;
};

/** v2.7：服务商下的模型行（服务商「模型」子列表的出参/入参形状） */
export type LlmGatewayModelRow = {
  id?: number;
  model_name: string;
  model_type: ModelType | "";
  /**
   * ⚠️ **v2.28 类型变更**：由 `string[]`（平台标识数组）改为**引用计数**——
   * `{"xhs": 2}` 表示「xhs 平台下有 2 个业务点正在用这个模型」。
   *
   * 用户口径（2026-10-03 原话）：
   * > 「所有使用平台字段应该存储为 **json，平台标识和使用次数**，当使用次数被减到为 0 时
   * > ……该平台可以被移除」。
   *
   * 出参恒为对象（后端 `_parse_platforms` 宽入：旧数组 `["xhs"]` ⇒ `{"xhs": 1}`）。
   */
  platform: Record<string, number>;
  /**
   * v2.28：`platform` 的**键列表**（后端同步下发）—— 渲染直接用，避免前端到处 `.Object.keys`。
   * 计数归 0 的平台**不出现在这里**（后端已丢弃）。
   */
  platforms: string[];
  enabled: boolean;
  remark?: string;
  /**
   * v2.10：**服务商内自然序号**（`"1"`/`"2"`/`"3"`…）—— 人类可读的唯一标识，
   * 区别于 DB 全局自增 `id`。同步/新增时后端按「组内最大 +1」生成，**不回收**；
   * 迁移前的老行由迁移回填。
   */
  model_key?: string;
  /**
   * v2.10：模型级健康（探测过且通 = true）。
   * ⚠️ 判「正常」须同时看 `last_health_at`（从未探测时它也是 false）。
   */
  healthy?: boolean;
  /** v2.10：模型级最近探测时间；`null` = 从未探测。 */
  last_health_at?: string | null;
  /**
   * v2.10：**正在使用该模型的业务点**（空数组 = 无业务点在用）。
   * 用户口径「已在使用的模型无法停用」⇒ 非空时前端置灰「启用」开关；
   * 后端 `PATCH` 也会 400 拦（`LLM_MODEL_IN_USE_BY_BUSINESS`，前端置灰只是体验层）。
   */
  usages?: LlmGatewayModelUsage[];
};

/** v2.10：一条「模型被某业务点使用」的记录（`LlmGatewayModelRow.usages` 的元素）。 */
export type LlmGatewayModelUsage = {
  /** 业务点固定 key（如 `generate_note`） */
  business_key: string;
  /** 业务点中文名（如「生成笔记」；页面提示用） */
  label: string;
  /** 业务点能力类别（`text` / `image` / `multimodal` / `video`） */
  kind?: string;
  /** 该映射所属平台 */
  platform?: string;
};

/**
 * v2.9：`POST /llm/gateway-configs/{id}/sync-models` 的返回体
 * （拉目录 + AI 归纳类型 + upsert 模型行）。
 *
 * ⚠️ **同步失败不是 HTTP 错误** —— 上游不可达 / 令牌过期时接口仍返 200，
 * 由 `sync.ok=false` + `sync.reason` 如实说明（服务商本身没问题，只是这次同步没成）。
 */
export type LlmGatewaySyncSummary = {
  /** 目录是否成功拉到且模型行已落库 */
  ok: boolean;
  /** 中文结果说明（成功时是「目录 N 个模型; 新建 X / 补类型 Y; AI 归纳 …」） */
  reason: string;
  /** 上游返回的模型个数 */
  catalog_count: number;
  /** 本次新建的模型行数 */
  created: number;
  /** 本次补上类型的已有行数（仅补空值，不覆盖用户手工改过的类型） */
  updated: number;
  /** AI 成功归纳出类型的模型个数 */
  classified: number;
  /** 参与归纳的模型总数 */
  total: number;
};

export type LlmGatewayConfigPayload = {
  name: string;
  base_url?: string;
  access_token?: string;
  /**
   * ⚠️ **v2.7/v2.8**：模型随服务商一并创建/更新 —— 每项带自己的 type/platform/enabled/remark。
   * `platform` / `model_type` / `default_model` 三键**不再是服务商级字段**：
   * 后端 `extra="forbid"`，传了会 **422**（§13.2.10）。
   */
  models?: LlmGatewayModelRow[];
  // ⚠️ 无 `provider_key`（v1.9）：该字段由服务端生成且**不可编辑** ——
  // 后端 PATCH 请求体带它会 422（`extra="forbid"`），前端不许出现在 payload 里。
};

// —— 「使用平台」枚举（GET /llm/providers/platforms，v1.9 D3 后端下发）——
export type LlmGatewayPlatformOption = {
  value: string;
  label: string;
};

// —— 用量统计（GET /llm/gateway-configs/{id}/stats；v1.6 起读本地台账聚合）——
export type LlmGatewayStatsBucket = {
  calls: number;
  success_calls: number;
  fail_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  quota: number;
  avg_latency_ms: number;
};

export type LlmGatewayStats = {
  id: number;
  available: boolean;
  message?: string;
  today: LlmGatewayStatsBucket | null;
  week: LlmGatewayStatsBucket | null;
};

// ⚠️ ~~`LlmGatewayToken` / `LlmGatewayTokenPayload`~~ **v1.9 删除**：令牌映射已整表退场
// （设计 17 §7.1a）。原来的两个类型 + `lib/api.ts` 的 4 个请求函数一并移除。

// —— 预警规则（设计 17 §7.1b，v1.6；v2.3 三级作用域）——
//
// 作用域由 `gateway_id` + `model_name` **两个字段共同表达**（后端不存 `scope` 枚举，
// 由 `_serialize_alert` 派生出只读的 `scope` 供列表渲染）：
//   · `gateway_id === null`  && `model_name === ""`  → **全网关**（逐服务商独立评估）
//   · `gateway_id` 为具体 id && `model_name === ""`  → **指定服务商**
//   · `gateway_id` 为具体 id && `model_name` 非空    → **指定服务商 + 具体模型**
// 唯一非法组合：全网关 + 指定模型（后端 400 `LLM_ALERT_SCOPE_CONFLICT`）。
export type LlmGatewayAlertScope = "all" | "gateway" | "model";

export type LlmGatewayAlert = {
  id: number;
  /** `null` = 全网关 */
  gateway_id: number | null;
  /** 只读派生字段：`all` / `gateway` / `model`（由后端序列化下发） */
  scope: LlmGatewayAlertScope;
  /** 空串 = 未限定具体模型（服务商级 / 全网关级） */
  model_name: string;
  platform: string;
  function: string;
  metric: string;
  operator: string;
  threshold: number;
  window_seconds: number;
  level: string;
  enabled: boolean;
  last_fired_at: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type LlmGatewayAlertPayload = {
  /** 省略 / `null` = 全网关 */
  gateway_id?: number | null;
  /** 非空 ⇒ 具体模型作用域（必须先指定服务商） */
  model_name?: string;
  platform?: string;
  function?: string;
  metric: string;
  operator: string;
  threshold: number;
  window_seconds?: number;
  level?: string;
  enabled?: boolean;
};

// —— 用量展示（设计 17 §7.5，v1.6）——
export type LlmGatewayUsageBucket = {
  calls: number;
  success: number;
  fail: number;
  fail_rate: number;
  total_tokens: number;
  avg_latency_ms: number;
};

export type LlmGatewayUsageOverview = {
  today: LlmGatewayUsageBucket;
  week: LlmGatewayUsageBucket;
  month: LlmGatewayUsageBucket;
  gateway_healthy: boolean | null;
};

export type LlmGatewayUsageDailyItem = {
  date: string;
  calls: number;
  success: number;
  fail: number;
  total_tokens: number;
  avg_latency_ms: number;
};

export type LlmGatewayUsageBreakdownItem = {
  key: string;
  calls: number;
  success: number;
  fail: number;
  fail_rate: number;
  total_tokens: number;
};

export type GenerateNotePayload = {
  platform?: "xhs";
  topic: string;
  reference?: string;
  instruction?: string;
};

export type GenerateTitlePayload = {
  title?: string;
  body: string;
  count?: number;
};

export type GenerateTagsPayload = {
  title?: string;
  body: string;
  count?: number;
};

export type GeneratedImageAsset = {
  id: number;
  draft_id?: number | null;
  prompt: string;
  model_name: string;
  params: Record<string, unknown>;
  file_path: string;
  created_at: string;
};

export type GenerateImagePayload = {
  prompt: string;
  reference_images?: string[];
  save_to_assets?: boolean;
};

export type GenerateImageResult = {
  url: string;
  raw?: unknown;
  asset?: GeneratedImageAsset;
};

export type UserImageFile = {
  file_name: string;
  url: string;
  size: number;
};

export type DescribeImagePayload = {
  image_url: string;
  instruction?: string;
};

/**
 * 队列**批次**的派生汇总（`GET /tasks?tree=1` 的批次行 `summary`，后端按子行现算）。
 *
 * 为什么由后端给：状态 / 计数 / 耗时的判据必须只有一份（后端 `batch_rollup`）——
 * 前端再算一遍就是 DEF-044（同一语义两份实现），迟早两边不一致。
 */
export type QueueBatchSummary = {
  /** 派生状态（与「可清理判据」自洽：不可清的明细 ⇒ 批次也不可清）。 */
  status: string;
  /** 明细总数（= 子行数）。 */
  total: number;
  done: number;
  /** `failed` 已把 `exhausted` 并进来（对用户是同一件事：没跑成，可重试）。 */
  failed: number;
  cancelled: number;
  running: number;
  pending: number;
  /** 已终态条数（进度分子）。 */
  settled: number;
  /** `settled / total` 的百分数。 */
  progress: number;
  /** 整批耗时（首条开始 → 最后一条结束）；还有未终态子行 ⇒ `null`（缺值口径）。 */
  duration_ms?: number | null;
};

export type TaskRecord = {
  id: number;
  platform: PlatformId;
  task_type: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "exhausted" | string;
  progress: number;
  payload: Record<string, unknown>;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  error_type?: string | null;
  retry_count?: number;
  max_retries?: number;
  parent_task_id?: number | null;
  /**
   * **唯一批次号**（只有批次父行有值，队列项为 `null`）。
   *
   * 「这一行是不是批次」由它回答（不看类型清单）：新队列上线时后端自动生成
   * 批次号，前端零改动就能把新队列的批次行渲染成父节点。
   */
  batch_no?: string | null;
  /** 批次行的派生汇总（只有批次行有；队列项行不带）。 */
  summary?: QueueBatchSummary | null;
  /**
   * 批次行**用到哪些平台账号**（后端 `batch_account_ids` 按子行派生：去重保序）。
   *
   * 只有批次行带（明细行的账号看自己 `payload.account_id`）。一次批量可以跨账号
   * 勾选 ⇒ 批次级不存在「唯一账号」，所以这里是**列表**而不是单个 id
   * （2026-09-26 用户口径「批次记录执行的账号」）。
   */
  accounts?: number[] | null;
  children?: TaskRecord[];
};

export type ScheduleStatus = {
  enabled: boolean;
  running: boolean;
  timezone: string;
  jobs: Array<{
    id: string;
    next_run_time?: string | null;
  }>;
  stats: {
    total: number;
    active: number;
    paused: number;
    completed: number;
  };
};

/** 调度任务行（若依 sys_job 的本项目化，设计 15 §4.1） */
export type ScheduledJob = {
  id: number;
  user_id: number | null;
  name: string;
  job_key: string;
  /** 任务类型三档（R14）：manual 只能手动触发 / cron 周期 / 定时一次；interval 已退役 */
  trigger_type: "manual" | "cron" | "at" | string;
  cron_expression: string;
  run_at?: string | null;
  misfire_policy: "fire_now" | "once" | "discard" | string;
  concurrent: "forbid" | "allow" | string;
  params: Record<string, unknown>;
  /** 用户可写的自定义参数键（注册表白名单；前端据此渲染对应表单，不硬编码键名） */
  params_editable: string[];
  status: "active" | "paused" | "completed" | string;
  remark: string;
  is_builtin: boolean;
  last_run_at?: string | null;
  next_run_at?: string | null;
  run_count: number;
  fail_count: number;
  consecutive_failures: number;
  backoff_until?: string | null;
  last_status?: string | null;
  created_at: string;
  updated_at?: string | null;
};

/** 可调度目标（R18：注册表全量 + 各目标已建任务数，设计 15 §5） */
export type ScheduleTarget = {
  job_key: string;
  name: string;
  builtin: boolean;
  allow_concurrent: boolean;
  params_required: string[];
  params_editable: string[];
  default_trigger_type: "manual" | "cron" | "at" | string;
  default_cron: string;
  default_misfire_policy: "fire_now" | "once" | "discard" | string;
  /** 当前用户（含公共行）在该目标下已有的任务数 */
  existing_count: number;
};

/** 调度执行日志（每次执行一行，设计 15 §4.2） */
export type ScheduledJobLog = {
  id: number;
  job_id: number | null;
  /**
   * 业务任务号（后端迁移 `k2l3m4n5o6p7`）：操作列「取消任务」按它调
   * `POST /tasks/{id}/cancel`。`null` = 这条日志没有对应业务任务行（cron 系统作业 /
   * 存量日志）⇒ **不渲染**取消按钮（缺值口径：不猜一个 id，猜错就会去取消别人的任务）。
   */
  task_id?: number | null;
  job_name: string;
  job_key: string;
  trigger_type: "scheduled" | "manual" | string;
  /** 执行时快照：任务的任务类型（manual/cron/at；旧日志为空串） */
  job_trigger_type: string;
  status: "running" | "success" | "failed" | "skipped" | string;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  /** 任务进度百分比 0~100（R13：日志表「任务进度 + 耗时或异常」列渲染进度条） */
  progress: number;
  message: string;
  exception_info: string;
  result: Record<string, unknown>;
  /** 执行那一刻的任务配置快照（详情弹窗用；本列新增前的执行为 null） */
  config_snapshot: Record<string, unknown> | null;
  created_at: string;
};

/** 调度设置（创建 / 更新共用；与后端 ScheduleIn 对齐） */
export type ScheduleIn = {
  trigger_type?: "manual" | "cron" | "at";
  cron_expression?: string;
  run_at?: string | null;
  misfire_policy?: "fire_now" | "once" | "discard";
  concurrent?: "forbid" | "allow";
  /** false = 建行即暂停（不自动执行） */
  enabled?: boolean;
};

/** auto_tasks 响应里的调度摘要（来自 1:1 关联的 job 行） */
export type AutoTaskSchedule = {
  job_id: number;
  trigger_type: string;
  cron_expression: string;
  run_at?: string | null;
  misfire_policy: string;
  concurrent: string;
  status: string;
  next_run_at?: string | null;
  last_run_at?: string | null;
  run_count: number;
  last_status?: string | null;
};

export type RunDueTasksResponse = {
  executed_count: number;
  failed_count: number;
  items: PublishJob[];
};

export type MonitoringTarget = {
  id: number;
  platform: PlatformId;
  target_type: "keyword" | "account" | "brand" | "note_url" | string;
  name: string;
  value: string;
  status: "active" | "paused" | string;
  config: Record<string, unknown>;
  last_refreshed_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type MonitoringTargetPayload = {
  target_type: "keyword" | "account" | "brand" | "note_url";
  name?: string;
  value: string;
  status?: "active" | "paused";
  config?: Record<string, unknown>;
};

export type MonitoringSnapshot = {
  id: number;
  target_id: number;
  payload: {
    matched_count?: number;
    total_engagement?: number;
    top_notes?: Array<{
      id: number;
      note_id: string;
      title: string;
      author_name: string;
      engagement: number;
    }>;
  };
  created_at: string;
};

export type MonitoringNote = {
  id: number;
  note_id: string;
  title: string;
  author_name: string;
  created_at: string;
  likes: number;
  collects: number;
  comments: number;
  shares: number;
  engagement: number;
};

export type MonitoringRefreshResponse = {
  target: MonitoringTarget;
  task: TaskRecord;
  snapshot: MonitoringSnapshot;
};

// 运行时配置中心（设计 10 §7，2026-09-27）：设置页的列表 / 增删改查。
// `value` 已由后端按 `value_type` 解码成 JS 值（int → number、bool → boolean），
// 页面**不要**再自己 `JSON.parse`（那是第二份解码实现，DEF-044）。
export type SystemSettingValueType = "int" | "float" | "bool" | "str" | "json";

export type SystemSetting = {
  id: number;
  key: string;
  value: unknown;
  raw_value: string;
  value_type: SystemSettingValueType | string;
  category: string;
  label: string;
  description: string;
  requires_restart: boolean;
  is_dangerous: boolean;
  // 该键是否**接进了运行时读点**（后端 `settings_service.LIVE_KEYS` 白名单派生）。
  // false = 仅存档，改了不影响运行时 —— 页面必须显式标注，不许让用户以为改了有用。
  is_live: boolean;
  live_read_point: string;
  updated_by: number | null;
  created_at: string | null;
  updated_at: string | null;
};

export type SystemSettingListResponse = {
  items: SystemSetting[];
  total: number;
  live_keys: string[];
};

export type SystemSettingCreatePayload = {
  key: string;
  value: unknown;
  value_type?: SystemSettingValueType | string;
  category?: string;
  label?: string;
  description?: string;
  requires_restart?: boolean;
  is_dangerous?: boolean;
};

export type SystemSettingUpdatePayload = {
  value?: unknown;
  value_type?: SystemSettingValueType | string;
  category?: string;
  label?: string;
  description?: string;
  requires_restart?: boolean;
  is_dangerous?: boolean;
};

export type SystemSettingDeleteResult = {
  key: string;
  status: string;
  effective_value: unknown;
  is_live: boolean;
};

export type KeywordGroup = {
  id: number;
  platform: PlatformId;
  name: string;
  keywords: string[];
  created_at: string;
  updated_at: string;
};

export type KeywordGroupPayload = {
  platform?: PlatformId;
  name: string;
  keywords: string[];
};

export type KeywordGroupDetail = KeywordGroup & {
  trend: {
    total_matches: number;
    total_engagement: number;
    keywords: Array<{ keyword: string; notes: number; engagement: number }>;
    matched_notes: Array<{
      id: number;
      note_id: string;
      title: string;
      author_name: string;
      engagement: number;
      created_at: string;
    }>;
  };
};

export type PublishJob = {
  id: number;
  platform_account_id: number;
  source_draft_id?: number | null;
  platform: PlatformId;
  title: string;
  body: string;
  publish_mode: "immediate" | "scheduled" | string;
  publish_options?: PublishOptions;
  status: "pending" | "uploading" | "publishing" | "scheduled" | "published" | "failed" | "cancelled" | string;
  scheduled_at?: string | null;
  external_note_id: string;
  publish_error: string;
  published_at?: string | null;
  created_at: string;
};

export type PublishOptions = {
  topics?: string[];
  location?: string;
  privacy_type?: 0 | 1 | number;
  is_private?: boolean;
  draft_tags?: Array<{ id?: string; name?: string }>;
};

export type SendDraftToPublishPayload = {
  platform_account_id?: number | null;
  publish_mode?: "immediate" | "scheduled";
  scheduled_at?: string | null;
  topics?: string[];
  location?: string | null;
  privacy_type?: 0 | 1 | null;
  is_private?: boolean | null;
};

export type PublishJobUpdatePayload = {
  title?: string;
  body?: string;
  platform_account_id?: number | null;
  publish_mode?: "immediate" | "scheduled";
  scheduled_at?: string | null;
  topics?: string[];
  location?: string | null;
  privacy_type?: 0 | 1 | null;
  is_private?: boolean | null;
};

export type PublishAsset = {
  id: number;
  publish_job_id: number;
  asset_type: "image" | "video" | string;
  file_path: string;
  upload_status: "pending" | "uploading" | "uploaded" | "failed" | string;
  creator_media_id: string;
  upload_error: string;
  creator_upload_info: Record<string, unknown>;
};

export type AutoTask = {
  id: number;
  user_id: number;
  name: string;
  keywords: string[];
  pc_account_id: number;
  creator_account_id: number;
  ai_instruction: string;
  status: "active" | "paused" | "completed" | string;
  last_run_at?: string | null;
  total_published: number;
  created_at: string;
  /** 1:1 关联的调度行摘要（设计 15 §7.1；调度语义的唯一权威在 job 行） */
  schedule?: AutoTaskSchedule | null;
};

export type AutoTaskCreatePayload = {
  name: string;
  keywords: string[];
  pc_account_id: number;
  creator_account_id: number;
  ai_instruction?: string;
  /** 不传 = 不自动执行（建行即暂停） */
  schedule?: ScheduleIn;
};

export type AutoTaskUpdatePayload = {
  name?: string;
  keywords?: string[];
  ai_instruction?: string;
  status?: "active" | "paused" | "completed";
  schedule?: ScheduleIn;
};

export type AutoTaskRunResult = {
  auto_task: AutoTask;
  log: ScheduledJobLog;
  detail: {
    keyword?: string;
    publish_mode?: string;
    source_note?: {
      note_id: string;
      title: string;
      likes: number;
      collects: number;
      comments: number;
    };
    draft?: {
      id: number;
      title: string;
      body: string;
      created_at: string;
    };
    publish_job?: {
      id: number;
      status: string;
      platform_account_id: number;
    };
  };
};

export type AppNotification = {
  id: number;
  title: string;
  body: string;
  level: "info" | "warning" | "error" | string;
  source_task_id?: number | null;
  source_type?: string | null;
  source_id?: number | null;
  read: boolean;
  created_at: string;
};

// ─────────────────────────── T9：LLM 生成链路（设计 17） ───────────────────────────

/** 素材文件夹（`GET /api/xhs/materials/folders` 元素，设计 17 §7.3）。 */
export type MaterialFolder = {
  id: number;
  name: string;
  path: string;
  enabled: boolean;
  /** 首次扫描还没跑完时为 null（**不是空串**：NULL 与「扫过但为 0」是两件事）。 */
  last_scan_at: string | null;
  file_count: number;
};

/** 素材文件（`GET /api/xhs/materials/files` 元素，设计 17 §7.3）。 */
export type MaterialFile = {
  id: number;
  folder_id: number;
  filename: string;
  /** `image` / `video`（后端落库时的枚举，前端按它分流缩略图）。 */
  file_type: string;
  rel_path: string;
  size_bytes: number;
  mtime: number;
  width: number | null;
  height: number | null;
  /** 缩略图相对 URL（视频无缩略图时为 null —— 设计即如此，不是缺陷）。 */
  thumb_url: string | null;
};

/** 提示词模块（可组合复用的片段，`prompt_modules`）。 */
export type PromptModule = {
  id: number;
  name: string;
  category: string;
  content: string;
  sort_order: number;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
};

/** 提示词模板里展开的模块引用（`_serialize_template` 的 `modules`）。 */
export type PromptTemplateModuleRef = {
  id: number;
  name: string;
  content: string;
  sort_order: number;
};

/** 提示词模板（两层结构的上层，`prompt_templates`）。 */
export type PromptTemplate = {
  id: number;
  name: string;
  /** 任务类型（与后端枚举同源，决定这个模板给哪条链路用）。 */
  task_type: string;
  /** 骨架正文，含 `{插槽}`；渲染时用模块内容填充。 */
  prompt_skeleton: string;
  module_ids: number[];
  is_default: boolean;
  version?: number;
  /** 仅列表端点展开（创建 / 更新响应里可能没有）。 */
  modules?: PromptTemplateModuleRef[];
  created_at?: string | null;
  updated_at?: string | null;
};

/** 媒体来源（设计 17 §13.4.1 / §13.4.4 v2.14：图片四选一 + 视频两选一）。 */
export type LlmMediaSource =
  | "product_image"
  | "material"
  | "reference_image"
  | "ai_generate"
  | "video_ai_generate"
  | "video_reference";

/** `media_source` 的参数载体（与来源一一对应，结构随来源变）。 */
export type LlmMediaOptions = {
  /** `material` 来源：勾选的素材文件 id。 */
  material_file_ids?: number[];
  /** `reference_image` / `ai_generate` / 视频来源：参考图 URL（为空则后端按台账首图取）。 */
  reference_image_urls?: string[];
  /** `ai_generate` / 视频来源：提示词（空则后端按商品拼默认）。 */
  prompt?: string;
  /** `ai_generate` 来源：生成张数（默认 1，上限 4）。 */
  count?: number;
  /** 视频来源：时长秒数（默认 5，上限 10）。 */
  duration?: number;
  /** 视频来源：分辨率（默认 `720x1280` 竖版）。 */
  size?: string;
};

/** LLM 生成队列快照（键名 `generated` —— 底座通用形状是 `synced`，此处显式改名）。 */
export type LlmGenerateQueueSnapshot = {
  generated: number;
  failed: number;
  total: number;
  running: number;
  queued: number;
  finished: LlmGenerateFinishedEntry[];
  today?: { ok: number; failed: number; pending: number };
};

/** 队列终态条目（失败原因**只在这里给一次**，不落笔记行）。 */
export type LlmGenerateFinishedEntry = {
  item_id: string;
  item_name?: string;
  status: string;
  note_id?: number | null;
  note_title?: string;
  reason?: string;
};

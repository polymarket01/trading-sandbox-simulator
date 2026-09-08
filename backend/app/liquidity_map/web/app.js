let enabledMakerSymbols = new Set();
/*
 * Liquidity Map Lab Lite 前端。
 *
 * 主视图是 Bookmap 风格的时间 × 价格挂单热图，叠加主动成交气泡；右侧是与主图
 * 共用同一 bucket 坐标的聚合 DOM 和价轴。默认实时盯盘只保留当前页面会话最近
 * 10 分钟，绝不读取磁盘历史；显式静态复盘会先冻结并关闭 WebSocket，再固定读取
 * 10 分钟 1 秒盘口历史和逐条成交事件历史。主图每个成交气泡只对应一条上游成交
 * 事件，前端绝不按时间、价格或方向求和合并。
 */

"use strict";

const MAX_TAS_ROWS = 260;
const MAX_TAS_BUFFER = 400;
const DESKTOP_AXIS_WIDTH = 78;
const DESKTOP_DOM_WIDTH = 128;
const COMPACT_AXIS_WIDTH = 68;
const COMPACT_DOM_WIDTH = 96;
const COMPACT_CHART_MAX_WIDTH = 720;
const PLOT_BOTTOM = 20;
const DEFAULT_PRICE_COVERAGE_PCT = 5;
const MIN_PRICE_COVERAGE_PCT = 0.05;
const MAX_PRICE_COVERAGE_PCT = 80;
// 跟随关闭后仍保留交易盯盘需要的单次边缘复位：进入上下 20% 时只平移
// 相机中心，回到 32% 内区后才释放，并用 1.5 秒冷却防止边缘抖动。
// 该状态机绝不改纵轴覆盖、锁定比例或 follow 开关。
const EDGE_RESET_TRIGGER_RATIO = 0.2;
const EDGE_RESET_RELEASE_RATIO = 0.32;
const EDGE_RESET_COOLDOWN_MS = 1500;
// 薄盘口相机不能追着每帧最外档做 exact-fit。首个有效盘口按
// mid 对称适配并留出约 15% 内部安全区，之后完全锁定纵轴比例。
// 后续范围扩大只提示超出视窗，不自动扩张或收缩；只有用户显式重新适配
// 才改变比例。这是覆盖范围变动时画面不跳的根本保证。
const BOOK_AUTO_FIT_INNER_RATIO = 0.85;
// “连续”显示只移动缓存位图，不需要追逐高刷屏。30 FPS 已足以保持平滑，
// 同时在横轴放大、每个物理像素都需重绘时把 Canvas 峰值工作量减半。
const FIXED_FRAME_RATE = 30;
const APP_MODE_LIVE = "live";
const APP_MODE_REPLAY = "replay";
const LIVE_MAX_LOOKBACK_MS = 10 * 60 * 1000;
const REPLAY_MAX_LOOKBACK_MS = 10 * 60 * 1000;
const HISTORY_MAX_LOOKBACK_MS = 10 * 60 * 1000;
const HISTORY_RESOLUTION_MS = 1000;
// 后端历史聚合把实时采样的短调度抖动视为连续。绘制层必须采用同一
// 容差，否则略晚到的正常相邻列会被错误画成黑色像素缝。
const LIVE_VISUAL_JITTER_MS = 100;
// “连续显示”只在实时盯盘生效。采样调度偶尔晚到时沿用上一份已确认盘口，
// 最多填到下一列自己的真实区间起点；超过 1.5 秒、显式 gap、序号
// 跳号以及 stream/grid 边界一律留黑，绝不拿未来快照反向补洞。
const LIVE_CONTINUOUS_HOLD_MAX_MS = 1500;
// 买一/卖一骨架只连接正常的小幅阶梯。异常快照若瞬间跨越大量价格格，深色
// 描边会画成贯穿热图的假黑缝；超过任一门槛就从新位置重新起笔，不伪造垂线。
const BOOK_LINE_MAX_BUCKET_JUMP = 8;
const BOOK_LINE_MAX_VERTICAL_JUMP_PX = 80;
const HISTORY_PREFETCH_RATIO = 0.35;
const HISTORY_PREFETCH_MIN_MS = 45 * 1000;
const HISTORY_PREFETCH_MAX_MS = 3 * 60 * 1000;
const HISTORY_REQUEST_DEBOUNCE_MS = 180;
// 盘口页仍由服务端 1.5MB / 1000 列门禁自行分页；前端只按 15 分钟从 NOW
// 向左分四个优先区间，避免固定 3 分钟切成约 20 组并在每组后串行等待成交。
const HISTORY_REQUEST_CHUNK_MS = 15 * 60 * 1000;
const HISTORY_WRITE_SETTLE_MS = 15 * 1000;
const HISTORY_MAX_COLUMNS = 1000;
const HISTORY_MAX_RESPONSE_BYTES = 1_500_000;
// 冷启动响应与服务端内存 ring 完全独立；服务端投影和 typed arrays 把浏览器
// 留存压在 100 万档以内，页数/字节只作为异常响应的第二道保护。
const HISTORY_MAX_TOTAL_RESPONSE_BYTES = 24_000_000;
const HISTORY_MAX_PAGES = 16;
const HISTORY_MAX_LEVELS_PER_COLUMN = 8192;
const HISTORY_REQUEST_MAX_LEVELS_PER_SIDE = 768;
const HISTORY_REQUEST_MIN_LEVELS_PER_SIDE = 64;
const HISTORY_MAX_TOTAL_LEVELS = 1_000_000;
const DEFAULT_TARGET = "sandbox";
const SUPPORTED_TARGETS = ["sandbox"];
const DEFAULT_ARCHIVE_MIN_NOTIONAL = 0;
const RAW_TRADE_PAGE_ITEMS = 5000;
// 10 分钟 1 秒列约 3600 个；再给服务端 3 分钟 1000ms 实时 ring（约 180 列）
// 和切换/分页边界留余量。不会把 10 分钟放大成实时频率常驻历史。
const MAX_COMBINED_COLUMNS = 4600;
// 现货式高频品种一小时在归档底线以上可能超过 3 万条。对象通道先保持有界，
// 但容量覆盖常见 10 分钟不过滤场景；命中上限必须显式进入 partial，绝不静默
// 冒充完整历史。后续若进一步扩大范围，应改为 SoA/typed-array 而不是继续加帽。
const MAX_COMBINED_BUBBLES = 100000;
// 高频币不应每个小批次都从数组头部 splice 并搬移近 10 万个引用。过窗事件
// 最多暂留 1023 条；达到阈值或接近硬帽时再一次性物理压缩，逐笔身份不变。
const BUBBLE_PREFIX_COMPACT_THRESHOLD = 1024;
// 气泡尺寸画像只使用通过显示门槛的当前市场合约逐条成交样本；过滤本身始终
// 使用绝对报价币名义金额，不看 p90。尺寸由 q50/q90/q99 合成稳健上尺度，主曲线采用凹形 log1p，
// 参考量级只到 22px，极端成交再缓慢逼近 30px，避免长尾重新挤成同尺寸。
const BUBBLE_SIZE_SAMPLE_WINDOW_MS = 10 * 60 * 1000;
const BUBBLE_SIZE_SAMPLE_DECAY_HALF_LIFE_MS = 5 * 60 * 1000;
const BUBBLE_SIZE_SAMPLE_LIMIT = 20000;
const BUBBLE_SIZE_PROFILE_INTERVAL_MS = 2000;
const BUBBLE_SIZE_SCALE_UP_TAU_MS = 20000;
const BUBBLE_SIZE_SCALE_DOWN_TAU_MS = 180000;
const BUBBLE_SIZE_DEADBAND_RATIO = 0.06;
const BUBBLE_SIZE_COLD_START_NOTIONAL = 50000;
const BUBBLE_SIZE_COLD_START_WEIGHT = 80;
const BUBBLE_SIZE_Q90_HEADROOM = 3;
const BUBBLE_SIZE_Q50_HEADROOM = 9;
const BUBBLE_SIZE_CURVE_GAIN = 9;
const BUBBLE_RADIUS_AT_REFERENCE_PX = 22;
const BUBBLE_DEFAULT_SCALE = 1.9;
const BUBBLE_RADIUS_MIN_PX = 2.2;
const BUBBLE_RADIUS_MAX_PX = 30;
const BUBBLE_RADIUS_UI_BOOST = 1.3;
const BUBBLE_RADIUS_DISPLAY_MAX_PX =
  BUBBLE_RADIUS_MAX_PX * BUBBLE_RADIUS_UI_BOOST;
const BUBBLE_SPRITE_PADDING_PX = 5;
const BUBBLE_RENDER_MARGIN_PX =
  BUBBLE_RADIUS_DISPLAY_MAX_PX + BUBBLE_SPRITE_PADDING_PX;
const DOM_ROW_ENCODING = "bntlru-v1";
const DOM_ROW_STRIDE = 6;
const DOM_ROW_BUCKET = 0;
const DOM_ROW_NOTIONAL = 1;
const DOM_ROW_TRUSTED = 2;
const DOM_ROW_RAW_LEVELS = 3;
const DOM_ROW_LOWER = 4;
const DOM_ROW_UPPER = 5;
const HEATMAP_LEVEL_ENCODING = "flat-pairs-v1";
const WIRE_PROTOCOL = "compact-v1";
const CENTER_EASING_MS = 150;
const HEATMAP_CACHE_PAD_Y = 72;
const HEATMAP_CACHE_REBUILD_SHIFT_PX = 48;
const LIVE_BOOK_GUTTER_RATIO = 0.18;
const LIVE_BOOK_GUTTER_MIN_PX = 96;
const LIVE_BOOK_GUTTER_MAX_PX = 200;
const LIVE_BOOK_OPACITY = 0.65;
const MIN_TIME_COL_WIDTH = 0.002;
const TIME_AXIS_STEPS_MS = [
  1000,
  2000,
  5000,
  10000,
  15000,
  30000,
  60000,
  120000,
  300000,
  600000,
  1800000,
  3600000,
];
const APP_BASE_PATH =
  location.pathname === "/" ? "" : location.pathname.replace(/\/+$/, "");
const PAGE_PARAMS = new URLSearchParams(location.search);
// 可分享 URL 只保留画面身份与本机验收参数。未知参数不继承到新地址，
// 避免把临时 token、跳转参数或旧版无效状态继续带入分享链接。
const PAGE_URL_PASSTHROUGH_KEYS = ["verify", "verifyColWidth"];
const VERIFY_MODE = PAGE_PARAMS.has("verify");
const VERIFY_COL_WIDTH = PAGE_PARAMS.has("verifyColWidth")
  ? Number(PAGE_PARAMS.get("verifyColWidth"))
  : null;

const state = {
  appMode: APP_MODE_LIVE,
  replayAsOfMs: null,
  replaySessionLimited: false,
  subscriptionEpoch: -1,
  streamEpoch: -1,
  target: DEFAULT_TARGET,
  symbol: "",
  meta: null,
  view: null,
  status: null,
  dom: null,
  mid: null,
  lastTrade: null,
  columns: [],
  bubbles: [],
  trades: [],
  maxColumns: 900,
  scaleLo: 0,
  scaleHi: 0,
  bubbleRef: 0,
  bubbleSizeRef: 0,
  bubbleSizeSamples: [],
  bubbleSizeProfile: null,
  bubbleSizeProfileAt: 0,
  bubbleSizeProfileVersion: 0,
  bubbleRevision: 0,
  uiRevision: 0,
  heatmapRevision: 0,
  domRevision: 0,
  gapReason: "",
  droppedTrades: 0,
  historyStatus: "live-disabled",
  historyColumns: 0,
  historyLookbackMs: LIVE_MAX_LOOKBACK_MS,
  tradeHistoryTruncated: false,
  tradeHistoryDropped: 0,
};

const viewport = {
  // 连续的用户目标；只允许初始化和滚轮修改，render 不能反写。
  coveragePct: DEFAULT_PRICE_COVERAGE_PCT,
  // 新市场默认自动适配薄盘口；任何人工纵轴缩放或平移都会关闭，盘口边界
  // 此后不能再修改交易员选择的相机。双击/双点价轴才显式恢复。
  autoFitBook: true,
  renderedCoveragePct: DEFAULT_PRICE_COVERAGE_PCT,
  renderedBookCoveragePct: null,
  bookOutsideViewport: false,
  priceScaleMode: "pending",
  // verifyColWidth 仅供本机验收复现最大横向放大，不改变普通页面默认值。
  colWidth:
    VERIFY_MODE && Number.isFinite(VERIFY_COL_WIDTH) && VERIFY_COL_WIDTH > 0
      ? Math.min(14, Math.max(1.2, VERIFY_COL_WIDTH))
      : 3,
  centerBucket: null,
  follow: true,
  dragging: false,
  lastY: 0,
  rowHeight: 4,
  edgeResetLatched: false,
  edgeResetCooldownUntil: 0,
  edgeRecenterCount: 0,
  centerFrameAt: 0,
};

const ui = {
  paused: false,
  tasThreshold: 10000,
  colorMode: "heatmap",
  heatContrast: 0,
  continuousHeatmap: true,
  bubbleScale: 1.9,
  // 主图按固定报价币名义金额门槛接收单条成交事件；复盘查询门槛另受归档底线钳制。
  // 浏览器入列前仍复核显示门槛，绝不能先把小单合并后再判断。
  bubbleFixedFloor: 5000,
};
const DENSITY_OPTIONS = [
  "fixed:0",
  "fixed:100",
  "fixed:1000",
  "fixed:5000",
  "fixed:20000",
  "fixed:50000",
];
const targetCatalog = new Map([
  ["sandbox", { id: "sandbox", label: "沙盒交易所", symbols: [] }],
  ["hyperliquid_perp", { id: "hyperliquid_perp", label: "Hyperliquid Perp", symbols: [] }],
]);

function loadPreference(key, fallback, allowed) {
  try {
    const value = window.localStorage.getItem(`lml.${key}`);
    if (value === null) return fallback;
    if (allowed && !allowed.includes(value)) return fallback;
    return value;
  } catch (error) {
    return fallback;
  }
}

function savePreference(key, value) {
  try {
    window.localStorage.setItem(`lml.${key}`, String(value));
  } catch (error) {
    /* 隐私模式下不可写，忽略即可 */
  }
}

function appPath(path) {
  return `${APP_BASE_PATH}${path}`;
}

function activeMaxLookbackMs() {
  if (state.appMode === APP_MODE_REPLAY) {
    return state.replaySessionLimited
      ? LIVE_MAX_LOOKBACK_MS
      : REPLAY_MAX_LOOKBACK_MS;
  }
  return LIVE_MAX_LOOKBACK_MS;
}

function activeTradeMinNotional() {
  const value = Number(ui.bubbleFixedFloor);
  return Number.isFinite(value) && value >= 0 ? value : 5000;
}

function archiveMinNotional(view = state.view) {
  const declared = Number(
    view &&
      (view.archiveMinNotional ?? view.rawTradeArchiveMinNotional)
  );
  return Number.isFinite(declared) && declared >= 0
    ? declared
    : DEFAULT_ARCHIVE_MIN_NOTIONAL;
}

function activeTradeQueryMinNotional() {
  return Math.max(activeTradeMinNotional(), archiveMinNotional());
}

function tradeFilterLabel(value = activeTradeMinNotional()) {
  const threshold = Number(value);
  return threshold > 0 ? `≥${fmtCompact(threshold)}` : "不过滤";
}

function markUiRevision() {
  state.uiRevision += 1;
}

const el = (id) => document.getElementById(id);
const canvas = el("chart");
const ctx = canvas.getContext("2d", { alpha: false });
const heatmapCanvas = document.createElement("canvas");
const heatmapCtx = heatmapCanvas.getContext("2d", { alpha: true });
const heatmapShiftCanvas = document.createElement("canvas");
const heatmapShiftCtx = heatmapShiftCanvas.getContext("2d", { alpha: true });
const liveBookCanvas = document.createElement("canvas");
const liveBookCtx = liveBookCanvas.getContext("2d", { alpha: true });

function chartSideWidths(width = canvas.getBoundingClientRect().width) {
  const compact = Number(width) <= COMPACT_CHART_MAX_WIDTH;
  return {
    axisWidth: compact ? COMPACT_AXIS_WIDTH : DESKTOP_AXIS_WIDTH,
    domWidth: compact ? COMPACT_DOM_WIDTH : DESKTOP_DOM_WIDTH,
  };
}

let renderQueued = false;
let forceRenderQueued = false;
let lastRenderedVisualFingerprint = "";
let nextVisualFrameAt = 0;
let socket = null;
let socketGeneration = 0;
let reconnectTimer = null;
let liveConnectionWanted = true;
let hasFirstFrame = false;
// 复盘必须先拿到实时首帧与网格身份才能冻结。URL 直达 replay 时先按
// 指定 target+symbol 建立一次实时连接，首帧到达后自动进入复盘。
let pendingInitialReplayMode = false;
let historyController = null;
let historyGeneration = 0;
let historyRequestSerial = 0;
let historyViewportTimer = null;
let bookHistoryLoadedRanges = [];
// 同一市场/冻结终点下，较低门槛的完整区间天然覆盖所有更高门槛。每个 entry
// 保存“至少 minNotional 的逐条成交已完整载入”的半开时间区间。
let tradeHistoryCoverage = [];
let historyInFlightRange = null;
let historyInFlightKind = null;
let historyInFlightTradeFloor = null;
let historyNoticeTimer = null;
let historyScheduleQueued = false;
let historyRefreshPending = false;
let historyQueuedDelayMs = HISTORY_REQUEST_DEBOUNCE_MS;
let historyPendingScope = null;
let historyLastRefreshAt = 0;
let pendingReconnectContinuation = false;
const historyLoadStats = {
  requests: 0,
  bookRequests: 0,
  tradeRequests: 0,
  cancellations: 0,
  coveredSkips: 0,
  completedChunks: 0,
  completedTradeChunks: 0,
  yieldedPages: 0,
  lastLevelsPerSide: 0,
};
let reconnectDelay = 500;
let transportConnected = false;
let liveBookFresh = false;
const visualClock = {
  newestColumnT: null,
  visualTimeMs: null,
  frameAt: 0,
  arrivedAt: 0,
};
const bubbleSpriteCache = new Map();
const heatmapCache = {
  key: "",
  center: null,
  cssWidth: 0,
  cssHeight: 0,
  revision: -1,
  latestSeq: null,
  latestT: null,
  horizontalResidualPx: 0,
  seamCorrections: 0,
  lastSeamCorrectionDevicePx: 0,
  maxSeamCorrectionDevicePx: 0,
  rebuilds: 0,
  incrementalUpdates: 0,
  subpixelTailUpdates: 0,
  lastPaintVisitedColumns: 0,
  lastPaintRenderedColumns: 0,
  temporalPixelSkips: 0,
  bookLinePixelSkips: 0,
  visualFrames: 0,
};
const liveBookCache = {
  key: "",
  center: null,
  cssWidth: 0,
  cssHeight: 0,
  rebuilds: 0,
};
const domMetricsCache = {
  revision: -1,
  dom: null,
  metrics: null,
};
const bubbleTradeIds = new Set();
let rejectedTradeEvents = 0;
let coverageDisplayKey = "";
let lastTrustedBookBounds = null;
const autoBookViewport = {
  marketKey: null,
  logHalfSpan: null,
  fitKind: null,
  sourceDomRevision: -1,
  lastObservedDomRevision: -1,
  lastObservedRequiredLogHalfSpan: null,
  scaleRevision: 0,
  refitPending: false,
};

function resetAutoBookViewport(preserveScale = false) {
  if (preserveScale && autoBookViewport.logHalfSpan > 0) {
    autoBookViewport.lastObservedDomRevision = -1;
    autoBookViewport.lastObservedRequiredLogHalfSpan = null;
    return;
  }
  autoBookViewport.marketKey = null;
  autoBookViewport.logHalfSpan = null;
  autoBookViewport.fitKind = null;
  autoBookViewport.sourceDomRevision = -1;
  autoBookViewport.lastObservedDomRevision = -1;
  autoBookViewport.lastObservedRequiredLogHalfSpan = null;
  autoBookViewport.scaleRevision = 0;
  autoBookViewport.refitPending = false;
}

function requestAutoBookViewportRefit(holdCoveragePct) {
  // 重新适配必须原子交换：在下一个 fresh DOM 到达前，用眼前已渲染
  // 覆盖建立临时锁定。不能先清空 logHalfSpan，否则断线/ready=false 时
  // 会瞬间退回隐藏的 5%，等 DOM 恢复后再突然缩回。
  const numericCoverage = Number(holdCoveragePct);
  if (Number.isFinite(numericCoverage) && numericCoverage > 0) {
    autoBookViewport.marketKey = `${state.target}\u001f${state.symbol}`;
    autoBookViewport.logHalfSpan = Math.log1p(numericCoverage / 100) / 2;
    autoBookViewport.fitKind = "hold";
  }
  autoBookViewport.refitPending = true;
}
let scaleInfoKey = "";
let contrastRenderTimer = null;
let pendingHeatContrast = null;
const frameStats = {
  startedAt: 0,
  frames: 0,
  fps: 0,
  lastRenderMs: 0,
  maxRenderMs: 0,
  skippedVisualFrames: 0,
};
// 气泡按三档顺序覆盖，但层数组和绘制项都跨帧复用。长时间运行后不再以
// 60 FPS 持续制造成千上万个短命对象，避免周期性垃圾回收顿挫。
const bubbleDrawLayers = [[], [], []];
const bubbleDrawItemPool = [];
const bubbleRenderStats = {
  lastVisited: 0,
  lastRendered: 0,
  lastVisibleFromMs: null,
};

/* ------------------------------------------------------------------ 工具 */

function priceDecimals() {
  if (state.meta && Number.isFinite(state.meta.priceDecimals)) {
    return Math.min(8, Math.max(0, Number(state.meta.priceDecimals)));
  }
  const tick = state.meta && state.meta.tickSize;
  if (tick != null && tick !== "") {
    const text = String(tick).toLowerCase();
    if (text.includes("e-")) {
      return Math.min(8, Number(text.split("e-")[1]) || 0);
    }
    const dot = text.indexOf(".");
    if (dot < 0) return 0;
    return Math.min(8, text.slice(dot + 1).replace(/0+$/, "").length || text.slice(dot + 1).length);
  }
  return 2;
}

function tickSize() {
  const tick = state.meta && Number(state.meta.tickSize);
  return tick > 0 ? tick : 0;
}

function fmtPrice(value) {
  const decimals = priceDecimals();
  let v = Number(value);
  if (!Number.isFinite(v)) return "--";
  const tick = tickSize();
  if (tick > 0) {
    v = Math.round(v / tick) * tick;
  }
  // 交易所精度固定补齐尾零：16.8 → 16.80
  return v.toFixed(decimals);
}

function fmtNotional(value) {
  const n = Math.round(Number(value) || 0);
  const sign = n < 0 ? "-" : "";
  return sign + Math.abs(n).toLocaleString("en-US");
}

function fmtCompact(value) {
  // 仅用于色阶摘要；TAS/COB 金额一律用 fmtNotional 完整数字。
  const v = Math.abs(Number(value) || 0);
  if (v >= 1e9) return (value / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (value / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (value / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

function fmtClock(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function domUsesFlatRows(dom) {
  return Boolean(
    dom &&
      dom.rowEncoding === DOM_ROW_ENCODING &&
      Number(dom.rowStride) === DOM_ROW_STRIDE
  );
}

function domSideRowCount(dom, side) {
  const rows = dom && Array.isArray(dom[side]) ? dom[side] : [];
  return domUsesFlatRows(dom)
    ? Math.floor(rows.length / DOM_ROW_STRIDE)
    : rows.length;
}

function forEachDomRow(dom, side, visit) {
  const rows = dom && Array.isArray(dom[side]) ? dom[side] : [];
  if (domUsesFlatRows(dom)) {
    for (let offset = 0; offset + DOM_ROW_STRIDE <= rows.length; offset += DOM_ROW_STRIDE) {
      visit(
        Number(rows[offset + DOM_ROW_BUCKET]),
        Number(rows[offset + DOM_ROW_NOTIONAL]),
        Boolean(rows[offset + DOM_ROW_TRUSTED]),
        Number(rows[offset + DOM_ROW_RAW_LEVELS]),
        rows[offset + DOM_ROW_LOWER],
        rows[offset + DOM_ROW_UPPER]
      );
    }
    return;
  }
  for (const row of rows) {
    visit(
      Number(row.bucket),
      Number(row.notionalValue),
      Boolean(row.trusted),
      Number(row.rawLevels),
      row.lower,
      row.upper
    );
  }
}

function firstDomRowPrice(dom, side, edge) {
  const rows = dom && Array.isArray(dom[side]) ? dom[side] : [];
  if (!rows.length) return null;
  if (domUsesFlatRows(dom)) {
    const offset = edge === "lower" ? DOM_ROW_LOWER : DOM_ROW_UPPER;
    const value = Number(rows[offset]);
    return Number.isFinite(value) ? value : null;
  }
  const value = Number(rows[0] && rows[0][edge]);
  return Number.isFinite(value) ? value : null;
}

function currentDomMetrics() {
  const dom = state.dom;
  if (
    domMetricsCache.revision === state.domRevision &&
    domMetricsCache.dom === dom &&
    domMetricsCache.metrics
  ) {
    return domMetricsCache.metrics;
  }
  const conservation = (dom && dom.conservation) || {};
  const totalBid = Number(conservation.bidAggregated || 0);
  const totalAsk = Number(conservation.askAggregated || 0);
  const metrics = {
    bidCount: domSideRowCount(dom, "bids"),
    askCount: domSideRowCount(dom, "asks"),
    bestBidUpper: firstDomRowPrice(dom, "bids", "upper"),
    bestAskLower: firstDomRowPrice(dom, "asks", "lower"),
    trustedLowerPrice: null,
    trustedUpperPrice: null,
    trustedLevelCount: 0,
    maxValue: 1,
    totalBid,
    totalAsk,
  };
  forEachDomRow(dom, "bids", (bucket, value, trusted, rawLevels, lower) => {
    if (!trusted) return;
    metrics.maxValue = Math.max(metrics.maxValue, value);
    const price = Number(lower);
    if (
      Number.isFinite(price) &&
      price > 0 &&
      (metrics.trustedLowerPrice === null || price < metrics.trustedLowerPrice)
    ) {
      metrics.trustedLowerPrice = price;
    }
    if (rawLevels > 0 || value > 0) metrics.trustedLevelCount += 1;
  });
  forEachDomRow(dom, "asks", (bucket, value, trusted, rawLevels, lower, upper) => {
    if (!trusted) return;
    metrics.maxValue = Math.max(metrics.maxValue, value);
    const price = Number(upper);
    if (
      Number.isFinite(price) &&
      price > 0 &&
      (metrics.trustedUpperPrice === null || price > metrics.trustedUpperPrice)
    ) {
      metrics.trustedUpperPrice = price;
    }
    if (rawLevels > 0 || value > 0) metrics.trustedLevelCount += 1;
  });
  domMetricsCache.revision = state.domRevision;
  domMetricsCache.dom = dom;
  domMetricsCache.metrics = metrics;
  return metrics;
}

function isFlatWireLevels(side) {
  return Boolean(
    Array.isArray(side) &&
      side.length % 2 === 0 &&
      (side.length === 0 || !Array.isArray(side[0]))
  );
}

function bucketToPrice(bucket) {
  const view = state.view;
  if (!view || !view.gridAnchor || !view.gridRatio) return null;
  return view.gridAnchor * Math.pow(view.gridRatio, bucket);
}

function priceToBucket(price) {
  const view = state.view;
  if (!view || !view.gridAnchor || !view.gridRatio) return null;
  return Math.log(price / view.gridAnchor) / Math.log(view.gridRatio);
}

function gridIdentity(view) {
  const anchor = Number(view && view.gridAnchor);
  const ratio = Number(view && view.gridRatio);
  const epoch = Number(view && view.gridEpoch);
  if (
    !Number.isFinite(anchor) ||
    anchor <= 0 ||
    !Number.isFinite(ratio) ||
    ratio <= 0 ||
    ratio === 1 ||
    !Number.isFinite(epoch)
  ) {
    return null;
  }
  return { anchor, ratio, epoch };
}

function gridPositionForPrice(price, grid) {
  const numeric = Number(price);
  if (!grid || !Number.isFinite(numeric) || numeric <= 0) return null;
  const position = Math.log(numeric / grid.anchor) / Math.log(grid.ratio);
  return Number.isFinite(position) ? position : null;
}

function priceForGridPosition(position, grid) {
  const numeric = Number(position);
  if (!grid || !Number.isFinite(numeric)) return null;
  const price = grid.anchor * Math.pow(grid.ratio, numeric);
  return Number.isFinite(price) && price > 0 ? price : null;
}

function gridBucketForPrice(price, grid) {
  const position = gridPositionForPrice(price, grid);
  return position === null ? null : Math.floor(position);
}

function reprojectGridBucket(bucket, oldGrid, newGrid) {
  const price = priceForGridPosition(bucket, oldGrid);
  return price === null ? null : gridBucketForPrice(price, newGrid);
}

function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  const next = sorted[Math.min(sorted.length - 1, base + 1)];
  return sorted[base] + (next - sorted[base]) * rest;
}

function weightedQuantile(sortedEntries, q, totalWeight) {
  if (!sortedEntries.length || !(totalWeight > 0)) return 0;
  const target = totalWeight * q;
  let cumulative = 0;
  for (const entry of sortedEntries) {
    cumulative += entry.weight;
    if (cumulative >= target) return entry.value;
  }
  return sortedEntries[sortedEntries.length - 1].value;
}

function bubbleSizeProfileForSamples(samples) {
  const valid = samples.filter(
    (sample) =>
      Number.isFinite(Number(sample.t)) &&
      Number.isFinite(Number(sample.notional)) &&
      Number(sample.notional) > 0
  );
  if (!valid.length) return null;
  const newestT = Math.max(...valid.map((sample) => Number(sample.t)));
  const weighted = valid.map((sample) => ({
    value: Number(sample.notional),
    weight: Math.pow(
      0.5,
      Math.max(0, newestT - Number(sample.t)) /
        BUBBLE_SIZE_SAMPLE_DECAY_HALF_LIFE_MS
    ),
  }));
  const effectiveSamples = weighted.reduce(
    (total, entry) => total + entry.weight,
    0
  );
  weighted.sort((left, right) => left.value - right.value);
  const q50 = weightedQuantile(weighted, 0.5, effectiveSamples);
  const q90 = weightedQuantile(weighted, 0.9, effectiveSamples);
  const q99 = weightedQuantile(weighted, 0.99, effectiveSamples);
  // 分布过窄或样本过少时保留上方空间；单笔离群值不能把普通成交推到 30px。
  const rawReference = Math.max(
    q99,
    q90 * BUBBLE_SIZE_Q90_HEADROOM,
    q50 * BUBBLE_SIZE_Q50_HEADROOM,
    1
  );
  const sampleWeight =
    effectiveSamples / (effectiveSamples + BUBBLE_SIZE_COLD_START_WEIGHT);
  const reference = Math.exp(
    Math.log(BUBBLE_SIZE_COLD_START_NOTIONAL) * (1 - sampleWeight) +
      Math.log(rawReference) * sampleWeight
  );
  return {
    q50,
    q90,
    q99,
    rawReference,
    reference,
    sampleCount: valid.length,
    effectiveSamples,
    newestT,
  };
}

function bubbleRadiusUnit(notional, reference) {
  const numericNotional = Math.max(0, Number(notional) || 0);
  const numericReference = Math.max(1, Number(reference) || 1);
  const ratio = numericNotional / numericReference;
  if (ratio <= 1) {
    return (
      BUBBLE_RADIUS_MIN_PX +
      (BUBBLE_RADIUS_AT_REFERENCE_PX - BUBBLE_RADIUS_MIN_PX) *
        (Math.log1p(BUBBLE_SIZE_CURVE_GAIN * ratio) /
          Math.log1p(BUBBLE_SIZE_CURVE_GAIN))
    );
  }
  // q99 以上使用渐近尾部：2×/10×/100× 参考量级约为 24.1/27.1/28.9px。
  return (
    BUBBLE_RADIUS_AT_REFERENCE_PX +
    (BUBBLE_RADIUS_MAX_PX - BUBBLE_RADIUS_AT_REFERENCE_PX) *
      (1 - Math.exp(-Math.log(ratio) / Math.log(10)))
  );
}

function bubbleRadiusPx(radiusUnit, scale) {
  const normalizedScale = Number(scale) / BUBBLE_DEFAULT_SCALE;
  const previousRadius = Math.max(
    BUBBLE_RADIUS_MIN_PX,
    Math.min(BUBBLE_RADIUS_MAX_PX, Number(radiusUnit) * normalizedScale)
  );
  // 四档 preference 值保持兼容；在旧最终半径（含 min/max clamp）之后统一放大 30%，
  // 因而每档、最小泡和封顶泡都严格是原尺寸的 1.3 倍。
  return previousRadius * BUBBLE_RADIUS_UI_BOOST;
}

function columnIntervalMs() {
  const configured = state.view && Number(state.view.heatmapColumnMs);
  return configured > 0 ? configured : 1000;
}

function bubbleEventTimeMs(bubble) {
  const cached = Number(bubble && bubble.displayEventTimeMs);
  if (Number.isFinite(cached)) return cached;
  const tradeTime = Number(bubble && bubble.tradeTime);
  return Number.isFinite(tradeTime) ? tradeTime : Number(bubble && bubble.t);
}

function lowerBoundBubbleEventTime(bubbles, timestamp) {
  let low = 0;
  let high = bubbles.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (bubbleEventTimeMs(bubbles[middle]) < timestamp) low = middle + 1;
    else high = middle;
  }
  return low;
}

function lowerBoundBubbleBucketTime(bubbles, timestamp) {
  let low = 0;
  let high = bubbles.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (Number(bubbles[middle].t) < timestamp) low = middle + 1;
    else high = middle;
  }
  return low;
}

function lowerBoundNumber(values, target) {
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (values[middle] < target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function mergeSortedBubbles(existing, incoming) {
  if (!incoming.length) return existing;
  if (!existing.length) return incoming;
  if (
    bubbleEventTimeMs(existing[existing.length - 1]) <=
    bubbleEventTimeMs(incoming[0])
  ) {
    existing.push(...incoming);
    return existing;
  }
  const merged = new Array(existing.length + incoming.length);
  let left = 0;
  let right = 0;
  let written = 0;
  while (left < existing.length || right < incoming.length) {
    if (
      right >= incoming.length ||
      (left < existing.length &&
        bubbleEventTimeMs(existing[left]) <=
          bubbleEventTimeMs(incoming[right]))
    ) {
      merged[written++] = existing[left++];
    } else {
      merged[written++] = incoming[right++];
    }
  }
  return merged;
}

function visualFrameIntervalMs() {
  return 1000 / FIXED_FRAME_RATE;
}

function visualFrameLabel() {
  return `${FIXED_FRAME_RATE} FPS`;
}

function timelineAnchorTimeMs() {
  const anchor =
    state.appMode === APP_MODE_REPLAY && Number.isFinite(Number(state.replayAsOfMs))
      ? Number(state.replayAsOfMs)
      : state.columns.length
        ? Number(state.columns[state.columns.length - 1].t)
        : NaN;
  return Number.isFinite(anchor) ? anchor : null;
}

function historicalXForTime(eventTimeMs, liveX, colWidth, timeShiftPx = 0) {
  const newestT = timelineAnchorTimeMs();
  if (!Number.isFinite(newestT)) return liveX;
  const pxPerMs = colWidth / columnIntervalMs();
  return liveX - (newestT - Number(eventTimeMs)) * pxPerMs - timeShiftPx;
}

function columnTimeInterval(columns, index) {
  const column = columns[index];
  const timestamp = Number(column && column.t);
  if (!Number.isFinite(timestamp)) return null;
  if (column && column.history) {
    const resolution = Math.max(
      1,
      Number(column.historyResolutionMs) || HISTORY_RESOLUTION_MS
    );
    // 历史 t 是 1 秒桶起点，必须画在 [t, t+resolution)；把 t 当右边界
    // 会让整段历史左移一秒，并在实时接管处产生视觉错位。
    return { fromMs: timestamp, toMs: timestamp + resolution };
  }
  // 实时 t 是配置周期列的右边界。即使中途断流，也不能把下一列向左
  // 拉伸到上一列结尾；那会伪造 sample-and-hold 覆盖，并错误挡住本可补齐
  // 的 1 秒磁盘历史。视觉延伸只能发生在绘制层，不能污染覆盖判定。
  return { fromMs: timestamp - columnIntervalMs(), toMs: timestamp };
}

function liveColumnsAreVisuallyContinuous(previous, column) {
  if (
    !previous ||
    !column ||
    previous.history ||
    column.history ||
    previous.gap ||
    column.gap ||
    !liveColumnsShareVisualIdentity(previous, column)
  ) {
    return false;
  }
  const previousT = Number(previous.t);
  const timestamp = Number(column.t);
  const previousSeq = Number(previous.seq);
  const sequence = Number(column.seq);
  const delta = timestamp - previousT;
  return (
    Number.isFinite(previousT) &&
    Number.isFinite(timestamp) &&
    Number.isFinite(previousSeq) &&
    Number.isFinite(sequence) &&
    sequence === previousSeq + 1 &&
    delta > 0 &&
    delta <= columnIntervalMs() + LIVE_VISUAL_JITTER_MS
  );
}

function liveColumnsShareVisualIdentity(previous, column) {
  if (!previous || !column) return false;
  const previousStream = Number(previous.liveStreamEpoch);
  const currentStream = Number(column.liveStreamEpoch);
  if (
    Number.isFinite(previousStream) &&
    Number.isFinite(currentStream) &&
    previousStream !== currentStream
  ) {
    return false;
  }
  const previousGrid = String(previous.liveGridKey || "");
  const currentGrid = String(column.liveGridKey || "");
  return !previousGrid || !currentGrid || previousGrid === currentGrid;
}

function liveColumnsCanSampleHold(previous, column) {
  if (
    !previous ||
    !column ||
    previous.history ||
    column.history ||
    previous.gap ||
    column.gap ||
    !liveColumnsShareVisualIdentity(previous, column)
  ) {
    return false;
  }
  const previousT = Number(previous.t);
  const timestamp = Number(column.t);
  const previousSeq = Number(previous.seq);
  const sequence = Number(column.seq);
  const delta = timestamp - previousT;
  return (
    Number.isFinite(previousT) &&
    Number.isFinite(timestamp) &&
    Number.isFinite(previousSeq) &&
    Number.isFinite(sequence) &&
    sequence === previousSeq + 1 &&
    delta > 0 &&
    delta <= LIVE_CONTINUOUS_HOLD_MAX_MS
  );
}

function liveContinuousHeatmapEnabled() {
  return state.appMode === APP_MODE_LIVE && ui.continuousHeatmap;
}

function bookLineBucketsAreVisuallyContinuous(
  previousBucket,
  currentBucket,
  rowHeight
) {
  const previous = Number(previousBucket);
  const current = Number(currentBucket);
  const pixelsPerBucket = Number(rowHeight);
  if (
    !Number.isFinite(previous) ||
    !Number.isFinite(current) ||
    !Number.isFinite(pixelsPerBucket) ||
    pixelsPerBucket <= 0
  ) {
    return false;
  }
  const bucketJump = Math.abs(current - previous);
  return (
    bucketJump <= BOOK_LINE_MAX_BUCKET_JUMP &&
    bucketJump * pixelsPerBucket <= BOOK_LINE_MAX_VERTICAL_JUMP_PX
  );
}

function previousLiveColumn(columns, index) {
  for (let previousIndex = index - 1; previousIndex >= 0; previousIndex -= 1) {
    if (!columns[previousIndex].history) return columns[previousIndex];
  }
  return null;
}

function nextLiveColumn(columns, index) {
  for (let nextIndex = index + 1; nextIndex < columns.length; nextIndex += 1) {
    if (!columns[nextIndex].history) return columns[nextIndex];
  }
  return null;
}

function columnPaintInterval(columns, index) {
  const interval = columnTimeInterval(columns, index);
  if (!interval) return interval;
  const column = columns[index];
  if (column && !column.history && liveContinuousHeatmapEnabled()) {
    const next = nextLiveColumn(columns, index);
    if (liveColumnsCanSampleHold(column, next)) {
      // 因果 sample-and-hold：只把当前已知快照向未来延伸到下一列自己的
      // 固定配置周期的区间起点；绝不把下一份未来快照向左拉来补过去。
      const heldToMs = Number(next.t) - columnIntervalMs();
      if (heldToMs > interval.toMs) {
        return { fromMs: interval.fromMs, toMs: heldToMs };
      }
    }
    return interval;
  }
  if (index <= 0) return interval;
  const previous = column && !column.history
    ? previousLiveColumn(columns, index)
    : columns[index - 1];
  if (!liveColumnsAreVisuallyContinuous(previous, column)) return interval;
  // 只在视觉层接住正常调度抖动；history/live 覆盖判定仍使用上面的
  // columnTimeInterval，真实断流不能因此挡住磁盘历史补齐。
  return { fromMs: Number(previous.t), toMs: interval.toMs };
}

function mergedLiveCoverageIntervals(columns) {
  const merged = [];
  for (let index = 0; index < columns.length; index += 1) {
    const column = columns[index];
    if (!column || column.history || column.gap) continue;
    const interval = columnTimeInterval(columns, index);
    if (
      !interval ||
      !Number.isFinite(interval.fromMs) ||
      !Number.isFinite(interval.toMs) ||
      interval.toMs <= interval.fromMs
    ) {
      continue;
    }
    const previous = merged.length ? merged[merged.length - 1] : null;
    if (previous && interval.fromMs <= previous.toMs) {
      previous.toMs = Math.max(previous.toMs, interval.toMs);
    } else {
      merged.push({ fromMs: interval.fromMs, toMs: interval.toMs });
    }
  }
  return merged;
}

function timeIntervalFullyCoveredByRanges(ranges, fromMs, toMs) {
  if (!(toMs > fromMs)) return false;
  let cursor = fromMs;
  for (const range of ranges) {
    if (range.toMs <= cursor) continue;
    if (range.fromMs > cursor) return false;
    cursor = Math.max(cursor, range.toMs);
    if (cursor >= toMs) return true;
  }
  return false;
}

function historyIntervalHasAnyLiveCoverage(liveColumns, fromMs, toMs) {
  if (!(toMs > fromMs) || !liveColumns.length) return false;
  const index = lowerBoundColumnTime(liveColumns, fromMs);
  for (const candidate of [index - 1, index, index + 1]) {
    if (candidate < 0 || candidate >= liveColumns.length) continue;
    const column = liveColumns[candidate];
    const interval = columnTimeInterval(liveColumns, candidate);
    if (
      interval &&
      !column.gap &&
      interval.fromMs < toMs &&
      interval.toMs > fromMs
    ) {
      return true;
    }
  }
  return false;
}

function timelineGeometry(plotRight, colWidth, timeShiftPx = 0) {
  const usableWidth = Math.max(0, plotRight);
  if (state.appMode === APP_MODE_REPLAY) {
    const liveX = snapToDevicePixel(usableWidth);
    const newestT = timelineAnchorTimeMs() ?? Date.now();
    const pxPerMs = colWidth / columnIntervalMs();
    return {
      liveX,
      gutterWidth: 0,
      newestT,
      pxPerMs,
      xForTime: (eventTimeMs) =>
        historicalXForTime(eventTimeMs, liveX, colWidth, 0),
    };
  }
  const preferredWidth = usableWidth * LIVE_BOOK_GUTTER_RATIO;
  // 极窄窗口允许缩到 72px；正常桌面宽度严格落在 96–200px。
  const responsiveMax = Math.min(
    usableWidth,
    Math.max(72, usableWidth * 0.32)
  );
  const responsiveMin = Math.min(LIVE_BOOK_GUTTER_MIN_PX, usableWidth);
  const gutterWidth = Math.min(
    LIVE_BOOK_GUTTER_MAX_PX,
    responsiveMax,
    Math.max(responsiveMin, preferredWidth)
  );
  // NOW 分界本身也落在整数物理像素上，避免历史区与当前盘口区在高 DPR
  // 屏幕上经过半像素混合后出现一条闪动暗缝。
  const liveX = snapToDevicePixel(usableWidth - gutterWidth);
  const alignedGutterWidth = Math.max(0, usableWidth - liveX);
  const newestT = timelineAnchorTimeMs() ?? Date.now();
  const pxPerMs = colWidth / columnIntervalMs();
  const xForTime = (eventTimeMs) =>
    historicalXForTime(eventTimeMs, liveX, colWidth, timeShiftPx);
  return {
    liveX,
    gutterWidth: alignedGutterWidth,
    newestT,
    pxPerMs,
    xForTime,
  };
}

function visualTimeAt(nowFrameMs = performance.now()) {
  if (
    !Number.isFinite(visualClock.visualTimeMs) ||
    visualClock.frameAt <= 0
  ) {
    return null;
  }
  return (
    visualClock.visualTimeMs +
    Math.max(0, nowFrameMs - visualClock.frameAt)
  );
}

function recordColumnArrival(columnTimeMs, nowFrameMs = performance.now()) {
  const previousVisualTime = visualTimeAt(nowFrameMs);
  visualClock.newestColumnT = Number(columnTimeMs);
  // 新列到达时保留上一帧已经走到的视觉时间，不再把插值相位归零。
  // 因而服务器采样间隔和网络延迟的微小波动不会变成一次反向纠偏。
  visualClock.visualTimeMs = Number.isFinite(previousVisualTime)
    ? previousVisualTime
    : Number(columnTimeMs);
  visualClock.frameAt = nowFrameMs;
  visualClock.arrivedAt = nowFrameMs;
}

function anchorVisualClockToNewest(nowFrameMs = performance.now()) {
  const newestT = state.columns.length
    ? Number(state.columns[state.columns.length - 1].t)
    : null;
  visualClock.newestColumnT = Number.isFinite(newestT) ? newestT : null;
  visualClock.visualTimeMs = Number.isFinite(newestT) ? newestT : null;
  visualClock.frameAt = Number.isFinite(newestT) ? nowFrameMs : 0;
  visualClock.arrivedAt = Number.isFinite(newestT) ? nowFrameMs : 0;
}

function visualTimeShiftPx(nowFrameMs = performance.now()) {
  if (!state.columns.length || !liveBookIsCurrent()) return 0;
  const newestT = Number(state.columns[state.columns.length - 1].t);
  if (
    !Number.isFinite(newestT) ||
    visualClock.newestColumnT !== newestT ||
    visualClock.arrivedAt <= 0
  ) {
    return 0;
  }
  const visualTime = visualTimeAt(nowFrameMs);
  if (!Number.isFinite(visualTime)) return 0;
  const rawShift =
    (visualTime - newestT) * (viewport.colWidth / columnIntervalMs());
  const limit = viewport.colWidth * 2.2;
  // 允许很短的负相位：新列比视觉时钟略早抵达时先在 NOW 外等待，旧画面仍
  // 连续前进；强行钳到 0 会重新引入肉眼可见的左右回弹。
  return Math.max(-limit, Math.min(limit, rawShift));
}

function shouldAnimateVisuals() {
  if (
    state.appMode !== APP_MODE_LIVE ||
    document.hidden ||
    !state.columns.length ||
    !liveBookIsCurrent()
  ) {
    return false;
  }
  const newestT = Number(state.columns[state.columns.length - 1].t);
  return (
    Number.isFinite(newestT) &&
    visualClock.newestColumnT === newestT &&
    performance.now() - visualClock.arrivedAt <= columnIntervalMs() * 2.2
  );
}

function queueNextVisualFrame() {
  if (
    renderQueued ||
    !shouldAnimateVisuals()
  ) {
    return;
  }
  // 直接挂到下一次屏幕刷新；requestRender 内部固定按 60 FPS 门槛跳帧。
  // 若这里再等待一个完整间隔，会叠加下一次 vsync，实际只剩约 18 FPS。
  requestRender(false);
}

function predictedCenterBucketAt(frameTime) {
  if (!viewport.follow || viewport.centerBucket === null || state.mid === null) {
    return viewport.centerBucket;
  }
  const targetBucket = priceToBucket(state.mid);
  if (targetBucket === null) return viewport.centerBucket;
  const elapsed =
    viewport.centerFrameAt > 0
      ? Math.max(0, Math.min(250, frameTime - viewport.centerFrameAt))
      : visualFrameIntervalMs() || 1000 / 60;
  const alpha = 1 - Math.exp(-elapsed / CENTER_EASING_MS);
  return viewport.centerBucket + (targetBucket - viewport.centerBucket) * alpha;
}

function visualFrameFingerprint(frameTime) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const shiftDevicePx = Math.round(
    alignedVisualShift(visualTimeShiftPx(frameTime)) * dpr
  );
  const predictedCenter = predictedCenterBucketAt(frameTime);
  const centerDevicePx = Number.isFinite(predictedCenter)
    ? Math.round(predictedCenter * Math.max(0.000001, viewport.rowHeight) * dpr)
    : "none";
  return [
    state.appMode,
    state.streamEpoch,
    state.heatmapRevision,
    state.domRevision,
    state.bubbleRevision,
    state.uiRevision,
    shiftDevicePx,
    centerDevicePx,
    canvas.width,
    canvas.height,
  ].join(":");
}

function requestRender(force = true) {
  if (force) forceRenderQueued = true;
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame((frameTime) => {
    renderQueued = false;
    const targetInterval = visualFrameIntervalMs();
    if (
      targetInterval > 0 &&
      nextVisualFrameAt > 0 &&
      frameTime < nextVisualFrameAt - 0.75
    ) {
      // 始终沿浏览器 vsync 取帧，避免 setTimeout 与高刷屏刷新节拍互相拍频。
      requestRender(false);
      return;
    }
    if (targetInterval > 0) {
      if (nextVisualFrameAt <= 0) {
        nextVisualFrameAt = frameTime + targetInterval;
      } else {
        const missed = Math.max(
          1,
          Math.floor((frameTime - nextVisualFrameAt) / targetInterval) + 1
        );
        nextVisualFrameAt += missed * targetInterval;
      }
    }
    const fingerprint = visualFrameFingerprint(frameTime);
    if (!forceRenderQueued && fingerprint === lastRenderedVisualFingerprint) {
      frameStats.skippedVisualFrames += 1;
      queueNextVisualFrame();
      return;
    }
    forceRenderQueued = false;
    const renderStartedAt = performance.now();
    render(frameTime);
    frameStats.lastRenderMs = performance.now() - renderStartedAt;
    frameStats.maxRenderMs = Math.max(
      frameStats.maxRenderMs,
      frameStats.lastRenderMs
    );
    lastRenderedVisualFingerprint = visualFrameFingerprint(frameTime);
    queueNextVisualFrame();
  });
}

function invalidateHeatmapCache() {
  heatmapCache.key = "";
}

function invalidateLiveBookCache() {
  liveBookCache.key = "";
}

/* -------------------------------------------------------------- 按需历史 */

function cancelHistoryLoad() {
  historyGeneration += 1;
  historyRequestSerial += 1;
  if (historyController) {
    historyLoadStats.cancellations += 1;
    historyController.abort();
  }
  if (historyViewportTimer !== null) window.clearTimeout(historyViewportTimer);
  historyController = null;
  historyViewportTimer = null;
  bookHistoryLoadedRanges = [];
  tradeHistoryCoverage = [];
  historyInFlightRange = null;
  historyInFlightKind = null;
  historyInFlightTradeFloor = null;
  historyScheduleQueued = false;
  historyRefreshPending = false;
  historyQueuedDelayMs = HISTORY_REQUEST_DEBOUNCE_MS;
  historyPendingScope = null;
  historyLastRefreshAt = 0;
  state.historyStatus =
    state.appMode === APP_MODE_REPLAY ? "idle" : "live-disabled";
  state.historyLookbackMs = activeMaxLookbackMs();
}

function resetTradeReservoirState() {
  state.tradeHistoryTruncated = false;
  state.tradeHistoryDropped = 0;
}

function mergeCoverageRange(ranges, fromMs, toMs) {
  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || toMs <= fromMs) {
    return ranges;
  }
  const ordered = ranges
    .concat([{ fromMs, toMs }])
    .sort((left, right) => left.fromMs - right.fromMs);
  const merged = [];
  for (const range of ordered) {
    const previous = merged[merged.length - 1];
    // 覆盖区间是严格半开区间；中间哪怕只差 1ms 也不能被合并成“完整”。
    if (!previous || range.fromMs > previous.toMs) {
      merged.push({ fromMs: range.fromMs, toMs: range.toMs });
      continue;
    }
    previous.toMs = Math.max(previous.toMs, range.toMs);
  }
  return merged;
}

function missingRangesForCoverage(ranges, fromMs, toMs) {
  if (!(toMs > fromMs)) return [];
  const missing = [];
  let cursor = fromMs;
  for (const range of ranges) {
    if (range.toMs <= cursor) continue;
    if (range.fromMs >= toMs) break;
    if (range.fromMs > cursor) {
      missing.push({ fromMs: cursor, toMs: Math.min(toMs, range.fromMs) });
    }
    cursor = Math.max(cursor, range.toMs);
    if (cursor >= toMs) break;
  }
  if (cursor < toMs) missing.push({ fromMs: cursor, toMs });
  // 靠近 NOW / 实时 ring 交界的缺口最先补，交易员先看到眼前连续区域。
  return missing.sort((left, right) => right.toMs - left.toMs);
}

function addBookHistoryLoadedRange(fromMs, toMs) {
  bookHistoryLoadedRanges = mergeCoverageRange(
    bookHistoryLoadedRanges,
    fromMs,
    toMs
  );
}

function missingBookHistoryRanges(fromMs, toMs) {
  return missingRangesForCoverage(bookHistoryLoadedRanges, fromMs, toMs);
}

function tradeCoverageEntry(minNotional, create = false) {
  const floor = Number(minNotional);
  let entry = tradeHistoryCoverage.find(
    (candidate) => Number(candidate.minNotional) === floor
  );
  if (!entry && create) {
    entry = { minNotional: floor, ranges: [] };
    tradeHistoryCoverage.push(entry);
    tradeHistoryCoverage.sort(
      (left, right) => Number(left.minNotional) - Number(right.minNotional)
    );
  }
  return entry || null;
}

function addTradeHistoryLoadedRange(minNotional, fromMs, toMs) {
  const entry = tradeCoverageEntry(minNotional, true);
  entry.ranges = mergeCoverageRange(entry.ranges, fromMs, toMs);
}

function combinedTradeCoverageRanges(minNotional) {
  const floor = Number(minNotional);
  let combined = [];
  for (const entry of tradeHistoryCoverage) {
    // 已载入 >=1000 的区间也完整覆盖 >=5000；反向不成立。
    if (Number(entry.minNotional) > floor) continue;
    for (const range of entry.ranges) {
      combined = mergeCoverageRange(combined, range.fromMs, range.toMs);
    }
  }
  return combined;
}

function missingTradeHistoryRanges(minNotional, fromMs, toMs) {
  return missingRangesForCoverage(
    combinedTradeCoverageRanges(minNotional),
    fromMs,
    toMs
  );
}

function tradeUpperExclusiveForRange(minNotional, fromMs, toMs) {
  const floor = Number(minNotional);
  let candidate = null;
  for (const entry of tradeHistoryCoverage) {
    const loadedFloor = Number(entry.minNotional);
    if (!(loadedFloor > floor)) continue;
    if (missingRangesForCoverage(entry.ranges, fromMs, toMs).length) continue;
    candidate = candidate === null ? loadedFloor : Math.min(candidate, loadedFloor);
  }
  return candidate;
}

function pruneHistoryLoadedRanges(newestT) {
  const cutoff = newestT - HISTORY_MAX_LOOKBACK_MS;
  bookHistoryLoadedRanges = bookHistoryLoadedRanges
    .filter((range) => range.toMs > cutoff)
    .map((range) => ({
      fromMs: Math.max(cutoff, range.fromMs),
      toMs: range.toMs,
    }));
  tradeHistoryCoverage = tradeHistoryCoverage
    .map((entry) => ({
      minNotional: entry.minNotional,
      ranges: entry.ranges
        .filter((range) => range.toMs > cutoff)
        .map((range) => ({
          fromMs: Math.max(cutoff, range.fromMs),
          toMs: range.toMs,
        })),
    }))
    .filter((entry) => entry.ranges.length > 0);
}

function showHistoryNotice(message) {
  const notice = el("historyNotice");
  notice.textContent = message;
  notice.classList.remove("hidden");
  if (historyNoticeTimer !== null) window.clearTimeout(historyNoticeTimer);
  historyNoticeTimer = window.setTimeout(() => {
    notice.classList.add("hidden");
    historyNoticeTimer = null;
  }, 5000);
}

function normalizedHistorySymbol(value) {
  // 保留 HIP-3 `dex:contract` 等有身份意义的分隔符；只做大小写无关比较并去空白。
  return String(value || "").trim().toUpperCase().replace(/\s+/g, "");
}

function normalizedTarget(value) {
  return String(value || "").trim().toLowerCase();
}

function rawTradeHistoryKind(view = state.view) {
  return String(view && view.rawTradeHistoryKind || "").trim();
}

function rawTradeSourceEventType(view = state.view) {
  return String(view && view.rawTradeSourceEventType || "").trim().toLowerCase();
}

function nearlyEqualNumber(left, right) {
  const a = Number(left);
  const b = Number(right);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return false;
  return Math.abs(a - b) <= Math.max(1e-12, Math.abs(a) * 1e-9);
}

function rawTradeHistoryAvailable(view = state.view) {
  return Boolean(
    view &&
      view.historyAvailable === true &&
      rawTradeHistoryKind(view) &&
      rawTradeSourceEventType(view)
  );
}

function currentHistoryIdentity() {
  if (state.appMode !== APP_MODE_REPLAY) return null;
  if (!Number.isFinite(Number(state.replayAsOfMs))) return null;
  // 旧进程只有 historyAvailable 布尔值，但没有动态逐条成交契约。必须同时看到
  // rawTradeHistoryKind 与 rawTradeSourceEventType，才允许请求磁盘历史；否则保持
  // session-only，绝不能撞新接口 404 或回退旧聚合气泡。
  if (!rawTradeHistoryAvailable(state.view)) return null;
  const liveColumns = state.columns.filter((column) => !column.history);
  const newest = Number(state.replayAsOfMs);
  const liveCutoffT = liveColumns.length
    ? Number(liveColumns[0].t)
    : newest;
  const anchor = Number(state.view && state.view.gridAnchor);
  const ratio = Number(state.view && state.view.gridRatio);
  const gridEpoch = Number(state.view && state.view.gridEpoch);
  const tick = Number(state.meta && state.meta.tickSize);
  if (
    !state.target ||
    !state.symbol ||
    !Number.isFinite(newest) ||
    !Number.isFinite(anchor) || anchor <= 0 ||
    !Number.isFinite(ratio) || ratio <= 0 || ratio === 1 ||
    !Number.isFinite(gridEpoch) ||
    !Number.isFinite(tick) || tick <= 0
  ) {
    return null;
  }
  return {
    generation: historyGeneration,
    symbol: state.symbol,
    normalizedSymbol: normalizedHistorySymbol(state.symbol),
    target: normalizedTarget(state.target),
    subscriptionEpoch: state.subscriptionEpoch,
    streamEpoch: state.streamEpoch,
    anchor,
    ratio,
    gridEpoch,
    tick,
    newestT: newest,
    asOfMs: newest,
    tradeKind: rawTradeHistoryKind(state.view),
    sourceEventType: rawTradeSourceEventType(state.view),
    archiveMinNotional: archiveMinNotional(state.view),
    liveCutoffT,
    gridKey: [anchor, ratio, gridEpoch, tick].join(":"),
  };
}

function historyIdentityIsCurrent(identity) {
  const current = currentHistoryIdentity();
  return !!(
    current &&
    identity.generation === historyGeneration &&
    identity.symbol === current.symbol &&
    identity.normalizedSymbol === current.normalizedSymbol &&
    identity.target === current.target &&
    identity.subscriptionEpoch === current.subscriptionEpoch &&
    identity.streamEpoch === current.streamEpoch &&
    identity.asOfMs === current.asOfMs &&
    identity.tradeKind === current.tradeKind &&
    identity.sourceEventType === current.sourceEventType &&
    identity.archiveMinNotional === current.archiveMinNotional &&
    identity.gridKey === current.gridKey
  );
}

async function readBoundedHistoryJson(response) {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > HISTORY_MAX_RESPONSE_BYTES) {
    throw new Error("历史响应超过 1.5MB 上限");
  }
  if (!response.body || !response.body.getReader) {
    const blob = await response.blob();
    if (blob.size > HISTORY_MAX_RESPONSE_BYTES) {
      throw new Error("历史响应超过 1.5MB 上限");
    }
    return { payload: JSON.parse(await blob.text()), bytes: blob.size };
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const chunks = [];
  let received = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > HISTORY_MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new Error("历史响应超过 1.5MB 上限");
      }
      chunks.push(decoder.decode(value, { stream: true }));
    }
    chunks.push(decoder.decode());
    return { payload: JSON.parse(chunks.join("")), bytes: received };
  } finally {
    reader.releaseLock();
  }
}

function validateReplayPayloadIdentity(payload, identity, fromMs, toMs) {
  const mode = String(payload && payload.mode || "").trim().toLowerCase();
  const responseAsOfMs = Number(payload && payload.asOfMs);
  const frozenToMs = Number(payload && payload.frozenToMs);
  const requestedFromMs = Number(payload && payload.requestedFromMs);
  const requestedToMs = Number(payload && payload.requestedToMs);
  if (
    state.appMode !== APP_MODE_REPLAY ||
    mode !== APP_MODE_REPLAY ||
    !historyIdentityIsCurrent(identity) ||
    responseAsOfMs !== Number(identity.asOfMs) ||
    !Number.isFinite(frozenToMs) ||
    !Number.isSafeInteger(requestedFromMs) ||
    !Number.isSafeInteger(requestedToMs) ||
    requestedFromMs !== Number(fromMs) ||
    requestedToMs !== Number(toMs) ||
    frozenToMs < Number(toMs) ||
    frozenToMs > Number(identity.asOfMs) ||
    Number(toMs) <= Number(fromMs)
  ) {
    throw new Error("复盘响应冻结身份不匹配");
  }
  return payload;
}

function historyLevelEntries(raw) {
  if (raw == null) return [];
  const entries = Array.isArray(raw) ? raw : Object.entries(raw);
  if (entries.length > HISTORY_MAX_LEVELS_PER_COLUMN) {
    throw new Error("历史单列档位超过保护上限");
  }
  return entries;
}

const EMPTY_HISTORY_LEVELS = Object.freeze({
  // 跨 series / 跨重启时旧网格档位在当前网格中通常落在小数位置。必须
  // 保留连续坐标；Int32 会再次向下量化，正是历史拼接差一整行的来源。
  buckets: new Float64Array(0),
  values: new Float32Array(0),
});

function isTypedHistoryLevels(side) {
  return Boolean(
    side &&
    (side.buckets instanceof Float64Array ||
      side.buckets instanceof Int32Array) &&
    side.values instanceof Float32Array &&
    side.buckets.length === side.values.length
  );
}

function makeTypedHistoryLevels(buckets, values, length) {
  if (length <= 0) return EMPTY_HISTORY_LEVELS;
  return {
    buckets: length === buckets.length ? buckets : buckets.slice(0, length),
    values: length === values.length ? values : values.slice(0, length),
  };
}

function projectionBucketForPrice(price, grid) {
  const position = gridPositionForPrice(price, grid);
  if (position === null) return null;
  // anchor * ratio ** integer 再取 log 时会出现 1e-12 级负误差。接近整数只
  // 吸附该整数；跨网格的真实小数位置必须保留，不能再 floor 到当前行。
  const nearest = Math.round(position);
  return Math.abs(position - nearest) <= 1e-9
    ? nearest
    : position;
}

function projectionCellSpan(sourceRatio, targetGrid) {
  const sourceLog = Math.log(Number(sourceRatio));
  const targetLog = Math.log(Number(targetGrid && targetGrid.ratio));
  if (
    !Number.isFinite(sourceLog) ||
    sourceLog <= 0 ||
    !Number.isFinite(targetLog) ||
    targetLog <= 0
  ) {
    return 1;
  }
  return Math.max(1e-6, sourceLog / targetLog);
}

function levelEntriesForProjection(side) {
  const entries = [];
  if (isTypedHistoryLevels(side)) {
    for (let index = 0; index < side.values.length; index += 1) {
      entries.push([side.buckets[index], side.values[index]]);
    }
  } else if (isFlatWireLevels(side)) {
    for (let index = 0; index < side.length; index += 2) {
      entries.push([side[index], side[index + 1]]);
    }
  } else if (Array.isArray(side)) {
    for (const entry of side) {
      if (Array.isArray(entry) && entry.length >= 2) {
        entries.push([entry[0], entry[1]]);
      }
    }
  } else {
    for (const entry of Object.entries(side || {})) entries.push(entry);
  }
  if (entries.length > HISTORY_MAX_LEVELS_PER_COLUMN) {
    throw new Error("历史单列档位超过保护上限");
  }
  return entries;
}

function captureLevelProjectionSource(side, grid) {
  if (!grid) return { prices: new Float64Array(0), values: new Float32Array(0) };
  const merged = new Map();
  for (const [rawBucket, rawValue] of levelEntriesForProjection(side)) {
    const bucket = Number(rawBucket);
    const value = Number(rawValue);
    const price = priceForGridPosition(bucket, grid);
    if (!Number.isFinite(bucket) || !Number.isFinite(value) || value <= 0 || price === null) {
      continue;
    }
    merged.set(price, (merged.get(price) || 0) + value);
  }
  const entries = Array.from(merged.entries()).sort(
    (left, right) => left[0] - right[0]
  );
  const prices = new Float64Array(entries.length);
  const values = new Float32Array(entries.length);
  for (let index = 0; index < entries.length; index += 1) {
    prices[index] = entries[index][0];
    values[index] = entries[index][1];
  }
  return { prices, values };
}

function projectLevelSource(source, newGrid) {
  const count = source && source.prices ? source.prices.length : 0;
  if (!count || !newGrid) return EMPTY_HISTORY_LEVELS;
  const buckets = new Float64Array(count);
  const values = new Float32Array(count);
  let written = 0;
  for (let index = 0; index < count; index += 1) {
    const bucket = projectionBucketForPrice(source.prices[index], newGrid);
    const value = Number(source.values[index]);
    if (bucket === null || !Number.isFinite(value) || value <= 0) continue;
    if (written > 0 && buckets[written - 1] === bucket) {
      values[written - 1] += value;
    } else {
      buckets[written] = bucket;
      values[written] = value;
      written += 1;
    }
  }
  return makeTypedHistoryLevels(buckets, values, written);
}

function captureColumnProjectionSource(column, sourceGrid) {
  if (!column || column.gap || column.projectionSource || !sourceGrid) return;
  const sourcePrice = (field) => {
    const raw = column[field];
    return raw === null || raw === undefined
      ? null
      : priceForGridPosition(raw, sourceGrid);
  };
  column.projectionSource = {
    gridRatio: Number(sourceGrid.ratio),
    centerPrice: sourcePrice("center"),
    bestBidPrice: sourcePrice("bestBid"),
    bestAskPrice: sourcePrice("bestAsk"),
    bids: captureLevelProjectionSource(column.bids, sourceGrid),
    asks: captureLevelProjectionSource(column.asks, sourceGrid),
  };
}

function reprojectColumnToGrid(column, oldGrid, newGrid) {
  if (!column || column.gap || !newGrid) return;
  // 旧运行态列没有 source 时只允许在第一次变化前从旧网格捕获；所有后续变化
  // 都从冻结的绝对价格重新投影，绝不从上次 floor 后的 bucket 级联投影。
  captureColumnProjectionSource(column, oldGrid);
  const source = column.projectionSource;
  if (!source) return;
  column.center = projectionBucketForPrice(source.centerPrice, newGrid);
  column.bestBid = projectionBucketForPrice(source.bestBidPrice, newGrid);
  column.bestAsk = projectionBucketForPrice(source.bestAskPrice, newGrid);
  column.bids = projectLevelSource(source.bids, newGrid);
  column.asks = projectLevelSource(source.asks, newGrid);
  column.gridCellSpan = projectionCellSpan(source.gridRatio, newGrid);
}

function reprojectBubbleToGrid(bubble, oldGrid, newGrid) {
  if (!bubble || !newGrid) return;
  const priceBucket = projectionBucketForPrice(bubble.price, newGrid);
  const nextBucket =
    priceBucket !== null
      ? priceBucket
      : oldGrid
        ? reprojectGridBucket(bubble.bucket, oldGrid, newGrid)
        : null;
  if (nextBucket !== null) bubble.bucket = nextBucket;
}

function liveColumnCadenceChanged(previousView, nextView) {
  const previousMs = Number(previousView && previousView.heatmapColumnMs);
  const nextMs = Number(nextView && nextView.heatmapColumnMs);
  return (
    Number.isFinite(previousMs) &&
    previousMs > 0 &&
    Number.isFinite(nextMs) &&
    nextMs > 0 &&
    previousMs !== nextMs
  );
}

function applyViewUpdate(viewPatch) {
  const previousView = state.view;
  const nextView = Object.assign({}, previousView, viewPatch);
  const columnCadenceChanged = liveColumnCadenceChanged(previousView, nextView);
  const historyWasAvailable = rawTradeHistoryAvailable(previousView);
  const historyIsAvailable = rawTradeHistoryAvailable(nextView);
  const oldGrid = gridIdentity(previousView);
  const newGrid = gridIdentity(nextView);
  const coordinatesChanged = Boolean(
    oldGrid &&
      newGrid &&
      (!nearlyEqualNumber(oldGrid.anchor, newGrid.anchor) ||
        !nearlyEqualNumber(oldGrid.ratio, newGrid.ratio))
  );
  const epochChanged = Boolean(
    oldGrid && newGrid && oldGrid.epoch !== newGrid.epoch
  );

  if (newGrid && (!oldGrid || coordinatesChanged || epochChanged)) {
    if (oldGrid && coordinatesChanged) {
      for (const column of state.columns) {
        reprojectColumnToGrid(column, oldGrid, newGrid);
      }
      if (viewport.centerBucket !== null) {
        const centerPrice = priceForGridPosition(viewport.centerBucket, oldGrid);
        const nextCenter = gridPositionForPrice(centerPrice, newGrid);
        if (nextCenter !== null) viewport.centerBucket = nextCenter;
      }
    }
    // 气泡本身携带真实成交价；包括服务器快照里跨 epoch 保留的气泡，都以价格重定位。
    for (const bubble of state.bubbles) {
      reprojectBubbleToGrid(bubble, oldGrid, newGrid);
    }
  }

  if (oldGrid && newGrid && (coordinatesChanged || epochChanged)) {
    // 旧 DOM、位图缓存和在途历史都绑定旧网格，不能继续与新 view 混画。
    state.dom = null;
    liveBookFresh = false;
    state.heatmapRevision += 1;
    state.domRevision += 1;
    viewport.centerFrameAt = performance.now();
    cancelHistoryLoad();
    invalidateHeatmapCache();
    invalidateLiveBookCache();
  }
  if (historyWasAvailable && !historyIsAvailable) {
    // 已加载内容仍是有效历史，保留在画面；这里只终止在途/后续读取并把
    // loader 复位。若稍后重新加入白名单，现有 message handler 会重新调度。
    cancelHistoryLoad();
  }
  if (columnCadenceChanged) {
    // live 列的 t 代表右边界，区间宽度来自 heatmapColumnMs。跨周期保留旧列会
    // 把旧周期列误解释为新周期并与新列重叠；时间语义变化时必须原子丢弃
    // 旧列和绑定它的视觉时钟/位图。逐事件成交使用真实毫秒时间，不受影响。
    cancelHistoryLoad();
    state.columns = [];
    state.historyColumns = 0;
    state.scaleLo = 0;
    state.scaleHi = 0;
    state.heatmapRevision += 1;
    visualClock.newestColumnT = null;
    visualClock.visualTimeMs = null;
    visualClock.frameAt = 0;
    visualClock.arrivedAt = 0;
    invalidateHeatmapCache();
  }
  state.view = nextView;
  return columnCadenceChanged;
}

function historyLevelCount(side) {
  if (isTypedHistoryLevels(side)) return side.values.length;
  if (isFlatWireLevels(side)) return side.length / 2;
  return side ? Object.keys(side).length : 0;
}

function validateHistorySeries(series, identity) {
  const symbol = normalizedHistorySymbol(series && series.symbol);
  const target = String(series && series.target || "").trim().toLowerCase();
  const tick = Number(series && series.tickSize);
  const anchor = Number(series && series.gridAnchor);
  const ratio = Number(series && series.gridRatio);
  const gridEpoch = Number(series && series.gridEpoch);
  if (
    symbol !== identity.normalizedSymbol ||
    target !== identity.target ||
    !nearlyEqualNumber(tick, identity.tick) ||
    !Number.isFinite(anchor) || anchor <= 0 ||
    !Number.isFinite(ratio) || ratio <= 0 || ratio === 1 ||
    !Number.isFinite(gridEpoch)
  ) {
    return null;
  }
  return { anchor, ratio, gridEpoch };
}

function remapHistoryBucket(rawBucket, oldGrid, identity) {
  if (rawBucket === null || rawBucket === undefined || rawBucket === "") return null;
  const bucket = Number(rawBucket);
  if (!Number.isFinite(bucket)) return null;
  const price = priceForGridPosition(bucket, oldGrid);
  return projectionBucketForPrice(price, {
    anchor: identity.anchor,
    ratio: identity.ratio,
    epoch: identity.gridEpoch,
  });
}

function remapHistoryLevels(raw, oldGrid, identity) {
  const source = captureLevelProjectionSource(raw, oldGrid);
  return projectLevelSource(source, {
    anchor: identity.anchor,
    ratio: identity.ratio,
    epoch: identity.gridEpoch,
  });
}

function freezeHistoricalScale(column) {
  if (column.gap) return;
  const capacity =
    historyLevelCount(column.bids) + historyLevelCount(column.asks);
  const values = new Float32Array(capacity);
  let written = 0;
  for (const side of [column.bids, column.asks]) {
    if (isTypedHistoryLevels(side)) {
      for (const value of side.values) {
        if (value > 0) values[written++] = value;
      }
    } else {
      for (const value of Object.values(side || {})) {
        if (value > 0) values[written++] = value;
      }
    }
  }
  const sorted = written === values.length ? values : values.slice(0, written);
  sorted.sort();
  const lo = Math.max(1, quantile(sorted, 0.55));
  const hi = Math.max(lo * 6, quantile(sorted, 0.985) * 3);
  column.displayScaleLo = lo;
  column.displayScaleHi = hi;
}

function historyGapColumn(timestamp) {
  return {
    t: Number(timestamp),
    gap: true,
    quality: "gap",
    partialCoverage: false,
    sourceCount: 0,
    coverageMs: 0,
    bids: EMPTY_HISTORY_LEVELS,
    asks: EMPTY_HISTORY_LEVELS,
    historyResolutionMs: HISTORY_RESOLUTION_MS,
    history: true,
  };
}

function historyCoverageKind(rawGap, rawQuality, retainedLevelCount) {
  const quality = String(rawQuality || "").trim().toLowerCase();
  const incomplete = Boolean(rawGap) || quality === "partial" || quality === "gap";
  if (!incomplete) return "complete";
  // 数据优先：旧服务只给 gap=true，但仍携带这一秒的有效均值盘口。
  // 这种列是不完整而非空白，不能因为少一个实时样本就整秒抹掉。
  return retainedLevelCount > 0 ? "partial" : "gap";
}

function normalizeHistoryColumn(raw, oldGrid, identity) {
  const timestamp = Number(raw && raw.t);
  if (!Number.isFinite(timestamp)) return null;
  const targetGrid = {
    anchor: identity.anchor,
    ratio: identity.ratio,
    epoch: identity.gridEpoch,
  };
  const bidSource = captureLevelProjectionSource(raw && raw.bids, oldGrid);
  const askSource = captureLevelProjectionSource(raw && raw.asks, oldGrid);
  const projectionSource = {
    gridRatio: Number(oldGrid.ratio),
    centerPrice: priceForGridPosition(raw && raw.center, oldGrid),
    bestBidPrice: priceForGridPosition(raw && raw.bestBid, oldGrid),
    bestAskPrice: priceForGridPosition(raw && raw.bestAsk, oldGrid),
    bids: bidSource,
    asks: askSource,
  };
  const bids = projectLevelSource(bidSource, targetGrid);
  const asks = projectLevelSource(askSource, targetGrid);
  const retainedLevelCount = historyLevelCount(bids) + historyLevelCount(asks);
  const quality = historyCoverageKind(
    raw && raw.gap,
    raw && raw.quality,
    retainedLevelCount
  );
  const column = {
    t: timestamp,
    gap: quality === "gap",
    quality,
    partialCoverage: quality === "partial",
    center: projectionBucketForPrice(projectionSource.centerPrice, targetGrid),
    bestBid: projectionBucketForPrice(projectionSource.bestBidPrice, targetGrid),
    bestAsk: projectionBucketForPrice(projectionSource.bestAskPrice, targetGrid),
    mid: Number.isFinite(Number(raw.mid)) ? Number(raw.mid) : null,
    bids,
    asks,
    sourceCount: Number(raw.sourceCount) || 0,
    coverageMs: Number(raw.coverageMs) || 0,
    historyResolutionMs: HISTORY_RESOLUTION_MS,
    // 旧 series 的一个价格格在当前网格里未必恰好等于一行。保留原始
    // 对数步长比例，绘制时同时保持价位下边界与单元高度连续。
    gridCellSpan: projectionCellSpan(oldGrid.ratio, targetGrid),
    history: true,
    projectionSource,
  };
  freezeHistoricalScale(column);
  return column;
}

function validatedHistoryGaps(payload, representedColumnCount) {
  const requestedFromMs = Number(payload && payload.requestedFromMs);
  const requestedToMs = Number(payload && payload.requestedToMs);
  const gaps = Array.isArray(payload && payload.gaps) ? payload.gaps : [];
  const validated = [];
  let declaredGapColumns = 0;
  for (const gap of gaps) {
    const start = Number(Array.isArray(gap) ? gap[0] : NaN);
    const end = Number(Array.isArray(gap) ? gap[1] : NaN);
    if (
      !Number.isSafeInteger(start) ||
      !Number.isSafeInteger(end) ||
      end <= start ||
      start < requestedFromMs ||
      end > requestedToMs
    ) {
      throw new Error("历史 gap 超出请求边界");
    }
    const gapColumns = Math.ceil((end - start) / HISTORY_RESOLUTION_MS);
    declaredGapColumns += gapColumns;
    if (representedColumnCount + declaredGapColumns > HISTORY_MAX_COLUMNS) {
      throw new Error("历史 gap 展开超过 1000 列上限");
    }
    validated.push([start, end]);
  }
  return validated;
}

function flattenHistoryPayload(payload, identity) {
  if (
    normalizedHistorySymbol(payload && payload.symbol) !== identity.normalizedSymbol ||
    String(payload && payload.target || "").trim().toLowerCase() !== identity.target
  ) {
    throw new Error("历史响应市场身份不匹配");
  }
  const rootSeries = payload && payload.series;
  const rootResolution = Number(payload && payload.resolutionMs);
  const tiles = Array.isArray(payload && payload.tiles)
    ? payload.tiles
    : [{
        columns: payload && payload.columns,
        series: rootSeries,
        resolutionMs: rootResolution,
      }];
  const columns = [];
  let retainedLevels = 0;
  let rawColumnCount = 0;
  let partial = Array.isArray(payload && payload.errors) && payload.errors.length > 0;
  for (const tile of tiles) {
    const rawColumns = Array.isArray(tile && tile.columns) ? tile.columns : [];
    rawColumnCount += rawColumns.length;
    if (rawColumnCount > HISTORY_MAX_COLUMNS) {
      throw new Error("历史响应超过 1000 列上限");
    }
    const resolution = Number(tile && tile.resolutionMs || rootResolution);
    const oldGrid = validateHistorySeries(
      tile && tile.series || rootSeries,
      identity
    );
    if (!oldGrid || resolution !== HISTORY_RESOLUTION_MS) {
      partial = partial || rawColumns.length > 0;
      for (const raw of rawColumns) {
        const timestamp = Number(raw && raw.t);
        if (Number.isFinite(timestamp)) {
          columns.push(historyGapColumn(timestamp));
        }
      }
      continue;
    }
    for (const raw of rawColumns) {
      const column = normalizeHistoryColumn(raw, oldGrid, identity);
      if (column) {
        retainedLevels +=
          historyLevelCount(column.bids) + historyLevelCount(column.asks);
        columns.push(column);
      }
    }
  }
  const representedTimestamps = new Set(columns.map((column) => Number(column.t)));
  for (const [start, end] of validatedHistoryGaps(payload, rawColumnCount)) {
    for (let timestamp = start; timestamp < end; timestamp += HISTORY_RESOLUTION_MS) {
      if (representedTimestamps.has(timestamp)) continue;
      columns.push(historyGapColumn(timestamp));
      representedTimestamps.add(timestamp);
    }
  }
  return { columns, partial, retainedLevels };
}

function lowerBoundColumnTime(columns, timestamp) {
  let low = 0;
  let high = columns.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (Number(columns[middle].t) < timestamp) low = middle + 1;
    else high = middle;
  }
  return low;
}

function pruneCombinedColumns(newestT) {
  // 实时页始终按会话十分钟硬裁剪；复盘页按显式范围裁剪。磁盘历史只会在
  // 复盘模式出现，因此普通盯盘不会因白名单能力不同而偷偷扩大常驻数组。
  const cutoff = newestT - activeMaxLookbackMs();
  const originalLength = state.columns.length;
  let written = 0;
  let historyCount = 0;
  for (let read = 0; read < originalLength; read += 1) {
    const existing = state.columns[read];
    const keep = read >= originalLength - 2 || Number(existing.t) >= cutoff;
    if (!keep) continue;
    state.columns[written++] = existing;
    if (existing.history) historyCount += 1;
  }
  state.columns.length = written;
  if (state.columns.length > MAX_COMBINED_COLUMNS) {
    const overflow = state.columns.length - MAX_COMBINED_COLUMNS;
    state.columns.splice(0, overflow);
    historyCount = 0;
    for (const existing of state.columns) {
      if (existing.history) historyCount += 1;
    }
  }
  state.historyColumns = historyCount;
}

function historyColumnsCoverInterval(columns, fromMs, toMs) {
  if (!(toMs > fromMs)) return false;
  let cursor = fromMs;
  let index = Math.max(0, lowerBoundColumnTime(columns, fromMs + 1) - 1);
  for (; index < columns.length; index += 1) {
    const column = columns[index];
    const start = Number(column.t);
    const end = start + HISTORY_RESOLUTION_MS;
    if (end <= cursor) continue;
    if (start >= toMs) break;
    // archive hard gap 不能参与覆盖；一旦它落在 live 的完整配置周期真值
    // 区间内，就必须保留该 live 列，而不是只看 live 右端时间戳。
    if (column.gap || start > cursor) return false;
    cursor = Math.max(cursor, end);
    if (cursor >= toMs) return true;
  }
  return false;
}

function columnExistsInInterval(columns, fromMs, toMs) {
  const index = lowerBoundColumnTime(columns, fromMs);
  return index < columns.length && Number(columns[index].t) < toMs;
}

function historyColumnQualityScore(column) {
  if (!column || column.gap) return 0;
  if (column.partialCoverage) return 1;
  return 2;
}

function preferredHistoryColumn(existingColumn, incomingColumn) {
  const existingScore = historyColumnQualityScore(existingColumn);
  const incomingScore = historyColumnQualityScore(incomingColumn);
  if (incomingScore !== existingScore) {
    return incomingScore > existingScore ? incomingColumn : existingColumn;
  }
  const existingCoverage = Number(existingColumn.coverageMs) || 0;
  const incomingCoverage = Number(incomingColumn.coverageMs) || 0;
  if (incomingCoverage !== existingCoverage) {
    return incomingCoverage > existingCoverage ? incomingColumn : existingColumn;
  }
  const existingSources = Number(existingColumn.sourceCount) || 0;
  const incomingSources = Number(incomingColumn.sourceCount) || 0;
  return incomingSources > existingSources ? incomingColumn : existingColumn;
}

function mergeSortedColumns(existing, incoming) {
  const merged = [];
  let left = 0;
  let right = 0;
  while (left < existing.length || right < incoming.length) {
    if (right >= incoming.length) {
      merged.push(existing[left++]);
      continue;
    }
    if (left >= existing.length) {
      merged.push(incoming[right++]);
      continue;
    }
    const existingT = Number(existing[left].t);
    const incomingT = Number(incoming[right].t);
    if (existingT === incomingT) {
      const existingColumn = existing[left];
      const incomingColumn = incoming[right];
      if (Boolean(existingColumn.history) === Boolean(incomingColumn.history)) {
        // settled-tail 可能把同一秒从 hard gap / partial 升级为更完整的
        // 历史列。优先质量、覆盖时长、样本数；完全相同才复用既有对象。
        merged.push(existingColumn.history
          ? preferredHistoryColumn(existingColumn, incomingColumn)
          : existingColumn);
        left += 1;
        right += 1;
      } else if (existingColumn.history) {
        // 同时刻固定 history 在前、live 在后；数组尾部与缓存 latestSeq
        // 因而始终落在更高分辨率的实时列上。
        merged.push(existingColumn);
        left += 1;
      } else {
        merged.push(incomingColumn);
        right += 1;
      }
    } else if (existingT < incomingT) {
      // 已缓存列优先，成功覆盖区间不会反复制造 typed arrays / 对象。
      merged.push(existing[left++]);
    } else {
      merged.push(incoming[right++]);
    }
  }
  return merged;
}

function mergeHistoricalColumns(columns, identity, fromMs, toMs) {
  if (!historyIdentityIsCurrent(identity)) return false;
  const incomingByTime = new Map();
  for (const column of columns) {
    const timestamp = Number(column.t);
    if (
      timestamp >= Math.max(fromMs, identity.asOfMs - activeMaxLookbackMs()) &&
      timestamp < toMs &&
      !incomingByTime.has(timestamp)
    ) {
      incomingByTime.set(timestamp, column);
    }
  }
  const incoming = Array.from(incomingByTime.values()).sort(
    (left, right) => Number(left.t) - Number(right.t)
  );
  if (!incoming.length) return true;

  const availableHistory = mergeSortedColumns(
    state.columns.filter((column) => column.history),
    incoming
  );
  // 复盘订单簿固定使用单一 1 秒历史源：complete 或仍携带真实档位的
  // partial 历史列一到，就替换同一秒已被覆盖的冻结 live 列。多分辨率叠画
  // 会把瞬时稀疏快照显示成竖向暗缝，也增加重绘成本。archive hard gap
  // 没有底图，才保留该秒仍然已知的冻结 live 真值，绝不凭空补数据。
  const retainedExisting = state.columns.filter((column) => {
    if (column.history) return true;
    const interval = columnTimeInterval([column], 0);
    return !interval || !historyColumnsCoverInterval(
      availableHistory,
      interval.fromMs,
      interval.toMs
    );
  });
  const retainedIncoming = incoming;

  state.columns = mergeSortedColumns(retainedExisting, retainedIncoming);
  const newest = Number(identity.asOfMs);
  const cutoff = identity.asOfMs - activeMaxLookbackMs();
  let trimCount = lowerBoundColumnTime(state.columns, cutoff);
  trimCount = Math.max(trimCount, state.columns.length - MAX_COMBINED_COLUMNS);
  if (trimCount > 0) state.columns.splice(0, trimCount);
  state.historyColumns = state.columns.filter((column) => column.history).length;

  const visibleFromMs = newest - visibleHistoryLookbackMs();
  if (toMs >= visibleFromMs - HISTORY_RESOLUTION_MS) {
    state.heatmapRevision += 1;
    invalidateHeatmapCache();
    requestRender();
  }
  return true;
}

async function requestHistoryRange(
  identity,
  fromMs,
  toMs,
  maxLevelsPerSide,
  signal
) {
  const params = new URLSearchParams({
    target: identity.target,
    symbol: identity.symbol,
    asOfMs: String(Math.floor(identity.asOfMs)),
    from: String(Math.floor(fromMs)),
    to: String(Math.floor(toMs)),
    maxColumns: String(HISTORY_MAX_COLUMNS),
    maxLevelsPerSide: String(maxLevelsPerSide),
    resolutionMs: String(HISTORY_RESOLUTION_MS),
  });
  const response = await fetch(
    `${appPath("/api/replay/history")}?${params}`,
    {
      signal,
      cache: "no-store",
      headers: { Accept: "application/json" },
    }
  );
  if (!response.ok) throw new Error(`历史接口 HTTP ${response.status}`);
  const decoded = await readBoundedHistoryJson(response);
  if (!historyIdentityIsCurrent(identity)) return null;
  const payload = validateReplayPayloadIdentity(
    decoded.payload,
    identity,
    fromMs,
    toMs
  );
  const flattened = flattenHistoryPayload(payload, identity);
  return {
    ...flattened,
    bytes: decoded.bytes,
    nextFromMs:
      payload && payload.nextFromMs != null
        ? Number(payload.nextFromMs)
        : null,
  };
}

function normalizeRawTradeCursor(raw) {
  if (raw === null || raw === undefined) return null;
  if (typeof raw !== "string") {
    throw new Error("逐条成交复合游标格式无效");
  }
  const cursor = raw.trim();
  if (!cursor || cursor.length > 256) {
    throw new Error("逐条成交复合游标格式无效");
  }
  return cursor;
}

function validateRawTradePayload(
  payload,
  identity,
  fromMs,
  toMs,
  queryMinNotional,
  maxNotionalExclusive = null
) {
  validateReplayPayloadIdentity(payload, identity, fromMs, toMs);
  if (
    String(payload && payload.kind || "").trim() !== identity.tradeKind ||
    String(payload && payload.sourceEventType || "").trim().toLowerCase() !==
      identity.sourceEventType ||
    payload.applicationAggregation !== false ||
    normalizedHistorySymbol(payload && payload.symbol) !== identity.normalizedSymbol ||
    String(payload && payload.target || "").trim().toLowerCase() !== identity.target ||
    Number(payload && payload.minNotional) !== Number(queryMinNotional) ||
    Number(payload && payload.archiveMinNotional) !==
      Number(identity.archiveMinNotional) ||
    !Array.isArray(payload && payload.trades) ||
    Object.prototype.hasOwnProperty.call(payload || {}, "bubbles")
  ) {
    throw new Error("复盘逐条成交契约不匹配");
  }
  // 新服务会回显上界；旧服务忽略未知 query 参数且不返回此字段。两种响应
  // 都可安全接收，因为旧服务只会多返回已按 ID 去重的大单，不会漏单。
  if (
    maxNotionalExclusive !== null &&
    Object.prototype.hasOwnProperty.call(payload || {}, "maxNotionalExclusive") &&
    Number(payload.maxNotionalExclusive) !== Number(maxNotionalExclusive)
  ) {
    throw new Error("复盘逐条成交上界契约不匹配");
  }
  return payload;
}

async function requestRawTradeHistoryRange(
  identity,
  fromMs,
  toMs,
  cursor,
  signal,
  queryMinNotional,
  maxNotionalExclusive = null
) {
  const params = new URLSearchParams({
    target: identity.target,
    symbol: identity.symbol,
    asOfMs: String(Math.floor(identity.asOfMs)),
    from: String(Math.floor(fromMs)),
    to: String(Math.floor(toMs)),
    minNotional: String(queryMinNotional),
    maxItems: String(RAW_TRADE_PAGE_ITEMS),
  });
  if (
    maxNotionalExclusive !== null &&
    Number.isFinite(Number(maxNotionalExclusive))
  ) {
    params.set("maxNotionalExclusive", String(maxNotionalExclusive));
  }
  if (cursor) {
    params.set("cursor", cursor);
  }
  const response = await fetch(
    `${appPath("/api/replay/trades")}?${params}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } }
  );
  if (!response.ok) throw new Error(`逐条成交历史接口 HTTP ${response.status}`);
  const decoded = await readBoundedHistoryJson(response);
  if (!historyIdentityIsCurrent(identity)) return null;
  const payload = validateRawTradePayload(
    decoded.payload || {},
    identity,
    fromMs,
    toMs,
    queryMinNotional,
    maxNotionalExclusive
  );
  return {
    trades: payload.trades,
    bytes: decoded.bytes,
    nextCursor: normalizeRawTradeCursor(payload.nextCursor),
  };
}

async function loadRawTradeHistoryPages(
  identity,
  fromMs,
  toMs,
  signal,
  queryMinNotional,
  maxNotionalExclusive = null
) {
  let cursor = null;
  let bytes = 0;
  let completed = fromMs >= toMs;
  let partial = false;
  let addedCount = 0;
  const droppedBefore = Number(state.tradeHistoryDropped) || 0;
  for (let page = 0; page < HISTORY_MAX_PAGES && !completed; page += 1) {
    // 筛选门槛在分页之间变化时，不再继续发旧门槛请求。提高门槛尤其要
    // fail-closed：迟到的低门槛页不能重新塞回刚清理过的小额历史气泡。
    if (Number(queryMinNotional) !== Number(activeTradeQueryMinNotional())) {
      return {
        bytes,
        completed: false,
        partial: true,
        nextCursor: cursor,
        addedCount,
      };
    }
    let result;
    try {
      result = await requestRawTradeHistoryRange(
        identity,
        fromMs,
        toMs,
        cursor,
        signal,
        queryMinNotional,
        maxNotionalExclusive
      );
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      return {
        bytes,
        completed: false,
        partial: true,
        nextCursor: cursor,
        addedCount,
      };
    }
    if (!result || !historyIdentityIsCurrent(identity)) {
      return {
        bytes,
        completed: false,
        partial: true,
        nextCursor: cursor,
        addedCount,
      };
    }
    historyLoadStats.tradeRequests += 1;
    bytes += result.bytes;
    const activeFloorAfterResponse = Number(activeTradeQueryMinNotional());
    if (Number(queryMinNotional) < activeFloorAfterResponse) {
      return {
        bytes,
        completed: false,
        partial: true,
        nextCursor: cursor,
        addedCount,
      };
    }
    let added;
    try {
      added = pushRawTradeBubbles(result.trades, {
        history: true,
        target: identity.target,
        symbol: identity.symbol,
        expectedSourceEventType: identity.sourceEventType,
        fromMs,
        toMs,
        strict: true,
        deferVisualCommit: true,
      });
    } catch (error) {
      // strict 校验在整页 normalize 完成前不会 merge；坏页自身零写入，但此前
      // 已安全载入的页必须把累计 addedCount 交回 chunk 统一提交，不能隐藏。
      return {
        bytes,
        completed: false,
        partial: true,
        nextCursor: cursor,
        addedCount,
      };
    }
    addedCount += added;
    if (!result.nextCursor) {
      completed = true;
      break;
    }
    if (result.nextCursor === cursor) {
      partial = true;
      break;
    }
    cursor = result.nextCursor;
    await yieldHistoryEventLoop(signal);
  }
  // 页数门禁命中时不能把未读逐条成交静默当完整区间；只允许标记 partial，
  // 绝不能退回 1 秒聚合气泡来补齐。
  if (!completed) partial = true;
  if ((Number(state.tradeHistoryDropped) || 0) > droppedBefore) partial = true;
  return { bytes, completed, partial, nextCursor: cursor, addedCount };
}

function commitDeferredBubbleVisuals(addedCount) {
  if (!(Number(addedCount) > 0)) return;
  state.bubbleRevision += 1;
  syncDensityControl();
  if (state.appMode === APP_MODE_REPLAY) requestRender();
}

function visibleHistoryLookbackMs() {
  if (!state.columns.length) return 0;
  const rect = canvas.getBoundingClientRect();
  const { axisWidth, domWidth } = chartSideWidths(rect.width);
  const plotRight = Math.max(0, rect.width - axisWidth - domWidth);
  const timeline = timelineGeometry(plotRight, viewport.colWidth, 0);
  if (!(timeline.pxPerMs > 0)) return 0;
  return Math.min(
    activeMaxLookbackMs(),
    Math.max(0, timeline.liveX / timeline.pxPerMs)
  );
}

function prefetchedHistoryLookbackMs() {
  if (state.appMode === APP_MODE_REPLAY) return activeMaxLookbackMs();
  const visibleMs = visibleHistoryLookbackMs();
  const prefetchMs = Math.min(
    HISTORY_PREFETCH_MAX_MS,
    Math.max(HISTORY_PREFETCH_MIN_MS, visibleMs * HISTORY_PREFETCH_RATIO)
  );
  return Math.min(activeMaxLookbackMs(), visibleMs + prefetchMs);
}

function historyRequestLevelsPerSide(lookbackMs) {
  const estimatedColumns = Math.max(
    1,
    Math.ceil(Number(lookbackMs) / HISTORY_RESOLUTION_MS)
  );
  const budget = Math.floor(
    HISTORY_MAX_TOTAL_LEVELS / (estimatedColumns * 2)
  );
  const rounded = Math.floor(budget / 64) * 64;
  return Math.max(
    HISTORY_REQUEST_MIN_LEVELS_PER_SIDE,
    Math.min(HISTORY_REQUEST_MAX_LEVELS_PER_SIDE, rounded || 64)
  );
}

function frozenBookHistoryToMs(identity, nowMs = Date.now()) {
  const frozenClosedToMs = Math.floor(
    Number(identity.asOfMs) / HISTORY_RESOLUTION_MS
  ) * HISTORY_RESOLUTION_MS;
  const settledToMs = Math.floor(
    (Number(nowMs) - HISTORY_WRITE_SETTLE_MS) / HISTORY_RESOLUTION_MS
  ) * HISTORY_RESOLUTION_MS;
  return Math.min(frozenClosedToMs, settledToMs);
}

function frozenBookHistorySettleDelayMs(identity, nowMs = Date.now()) {
  const frozenClosedToMs = Math.floor(
    Number(identity.asOfMs) / HISTORY_RESOLUTION_MS
  ) * HISTORY_RESOLUTION_MS;
  const delay = frozenClosedToMs + HISTORY_WRITE_SETTLE_MS - Number(nowMs);
  return Math.max(0, delay + HISTORY_REQUEST_DEBOUNCE_MS);
}

function historyRequestIsCurrent(identity, requestSerial, signal) {
  return (
    !signal.aborted &&
    requestSerial === historyRequestSerial &&
    historyIdentityIsCurrent(identity)
  );
}

function yieldHistoryEventLoop(signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("历史请求已取消", "AbortError"));
      return;
    }
    window.setTimeout(() => {
      if (signal.aborted) {
        reject(new DOMException("历史请求已取消", "AbortError"));
        return;
      }
      historyLoadStats.yieldedPages += 1;
      resolve();
    }, 0);
  });
}

async function loadBookHistoryPages(
  identity,
  requestSerial,
  fromMs,
  toMs,
  maxLevelsPerSide,
  signal,
  budget
) {
  let pageFromMs = fromMs;
  let loadedColumnCount = 0;
  let partial = false;
  let completed = false;
  let pendingColumns = [];
  for (let page = 0; page < HISTORY_MAX_PAGES; page += 1) {
    let result;
    try {
      result = await requestHistoryRange(
        identity,
        pageFromMs,
        toMs,
        maxLevelsPerSide,
        signal
      );
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      partial = true;
      break;
    }
    if (!result || !historyRequestIsCurrent(identity, requestSerial, signal)) {
      return { completed: false, partial: true, loadedColumnCount };
    }
    historyLoadStats.requests += 1;
    historyLoadStats.bookRequests += 1;
    budget.bytes += result.bytes;
    budget.levels += result.retainedLevels;
    if (
      budget.bytes > HISTORY_MAX_TOTAL_RESPONSE_BYTES ||
      budget.levels > HISTORY_MAX_TOTAL_LEVELS
    ) {
      partial = true;
      break;
    }
    pendingColumns = mergeSortedColumns(pendingColumns, result.columns);
    loadedColumnCount = pendingColumns.length;
    partial = partial || result.partial;
    if (!Number.isFinite(result.nextFromMs) || result.nextFromMs >= toMs) {
      completed = true;
      break;
    }
    if (result.nextFromMs <= pageFromMs) {
      partial = true;
      break;
    }
    pageFromMs = result.nextFromMs;
    await yieldHistoryEventLoop(signal);
  }
  // 一个15分钟优先区间只合并/失效/重画一次。网络或页契约在后页失败时，
  // 此前已完整验证的列仍会安全提交；坏页自身从未加入 pendingColumns。
  if (
    pendingColumns.length &&
    !mergeHistoricalColumns(pendingColumns, identity, fromMs, toMs)
  ) {
    return { completed: false, partial: true, loadedColumnCount: 0 };
  }
  return { completed, partial, loadedColumnCount };
}

async function loadHistoryForViewport(requestSerial) {
  const identity = currentHistoryIdentity();
  if (!identity || requestSerial !== historyRequestSerial) return;
  const requestScope = historyPendingScope === "trades" ? "trades" : "all";
  historyPendingScope = null;
  identity.generation = historyGeneration;
  const desiredLookbackMs = activeMaxLookbackMs();
  const bookFromMs = Math.max(
    identity.asOfMs - activeMaxLookbackMs(),
    identity.newestT - desiredLookbackMs
  );
  // 先读已经落稳的冻结区间，包括与实时 ring 重叠的部分；重叠历史只作
    // 底层兜底，实时列仍覆盖其上。冻结尾段尚未落稳时绝不提前登记
  // loaded，稍后只补这一段，避免部分实时覆盖留下无法再修复的黑缝。
  const frozenBookToMs = Math.floor(
    identity.asOfMs / HISTORY_RESOLUTION_MS
  ) * HISTORY_RESOLUTION_MS;
  const bookToMs = frozenBookHistoryToMs(identity);
  const needsFrozenTailBackfill = bookToMs < frozenBookToMs;
  // 成交归档与盘口 writer 独立，逐条历史必须覆盖到冻结 asOf；最近实时尾段
  // 由页面保留的原始单笔 reservoir 精确去重接棒，不再止于盘口 takeover。
  const tradeFromMs = identity.asOfMs - activeMaxLookbackMs();
  const tradeToMs = identity.asOfMs;
  pruneHistoryLoadedRanges(identity.newestT);
  const initialTradeFloor = activeTradeQueryMinNotional();
  const allMissingBook = bookToMs > bookFromMs
    ? missingBookHistoryRanges(bookFromMs, bookToMs)
    : [];
  const missingBook = requestScope === "trades" ? [] : allMissingBook;
  const missingTrades = missingTradeHistoryRanges(
    initialTradeFloor,
    tradeFromMs,
    tradeToMs
  );
  if (!missingBook.length && !missingTrades.length) {
    historyLoadStats.coveredSkips += 1;
    state.historyStatus = state.tradeHistoryTruncated || allMissingBook.length
      ? "partial"
      : state.historyColumns > 0 || state.bubbles.length > 0
        ? "ready"
        : "warming";
    // 即便本轮没有可读缺口，也要推进本次静态复盘的节流时钟，避免交互
    // 重复排入同一份已经确认覆盖的冻结区间。
    historyLastRefreshAt = Date.now();
    historyScheduleQueued = false;
    if (needsFrozenTailBackfill && currentHistoryIdentity()) {
      scheduleHistoryForViewport(
        frozenBookHistorySettleDelayMs(identity)
      );
    }
    return;
  }

  historyController = new AbortController();
  const signal = historyController.signal;
  const maxLevelsPerSide = historyRequestLevelsPerSide(desiredLookbackMs);
  historyLoadStats.lastLevelsPerSide = maxLevelsPerSide;
  state.historyStatus = "loading";
  historyLastRefreshAt = Date.now();
  const bookBudget = { bytes: 0, levels: 0 };
  const tradeBudget = { bytes: 0 };
  let loadedColumnCount = 0;
  let partial = false;
  try {
    // 第一管线只补盘口：四个 15 分钟优先区间从 NOW 向左，每个区间内部
    // 继续服从服务端分页。成交不再夹在每一页后面阻塞下一块盘口。
    for (const range of missingBook) {
      for (let chunkToMs = range.toMs; chunkToMs > range.fromMs;) {
        const chunkFromMs = Math.max(
          range.fromMs,
          chunkToMs - HISTORY_REQUEST_CHUNK_MS
        );
        historyInFlightRange = { fromMs: chunkFromMs, toMs: chunkToMs };
        historyInFlightKind = "book";
        historyInFlightTradeFloor = null;
        const result = await loadBookHistoryPages(
          identity,
          requestSerial,
          chunkFromMs,
          chunkToMs,
          maxLevelsPerSide,
          signal,
          bookBudget
        );
        if (!historyRequestIsCurrent(identity, requestSerial, signal)) return;
        loadedColumnCount += result.loadedColumnCount;
        partial = partial || result.partial;
        // 完整返回但没有列，表示该冷启动旧区间已确认无数据；同样记入
        // 覆盖范围，避免每 5 秒反复读取永远不会再补写的过去空块。
        if (result.completed && !result.partial) {
          addBookHistoryLoadedRange(chunkFromMs, chunkToMs);
          historyLoadStats.completedChunks += 1;
        } else {
          partial = true;
        }
        chunkToMs = chunkFromMs;
        await yieldHistoryEventLoop(signal);
      }
      if (bookBudget.bytes > HISTORY_MAX_TOTAL_RESPONSE_BYTES) break;
    }

    // 第二管线独立补逐条成交，范围一直到冻结 asOf。过滤在盘口加载期间变化
    // 时读取最新门槛；在成交页进行中再次降低则由 finally 合并补跑。
    const tradeFloor = activeTradeQueryMinNotional();
    const tradeMissingNow = missingTradeHistoryRanges(
      tradeFloor,
      tradeFromMs,
      tradeToMs
    );
    for (const range of tradeMissingNow) {
      for (let chunkToMs = range.toMs; chunkToMs > range.fromMs;) {
        const chunkFromMs = Math.max(
          range.fromMs,
          chunkToMs - HISTORY_REQUEST_CHUNK_MS
        );
        const maxNotionalExclusive = tradeUpperExclusiveForRange(
          tradeFloor,
          chunkFromMs,
          chunkToMs
        );
        historyInFlightRange = { fromMs: chunkFromMs, toMs: chunkToMs };
        historyInFlightKind = "trades";
        historyInFlightTradeFloor = tradeFloor;
        let result;
        try {
          result = await loadRawTradeHistoryPages(
            identity,
            chunkFromMs,
            chunkToMs,
            signal,
            tradeFloor,
            maxNotionalExclusive
          );
        } catch (error) {
          if (error && error.name === "AbortError") throw error;
          result = { bytes: 0, completed: false, partial: true };
        }
        if (!historyRequestIsCurrent(identity, requestSerial, signal)) return;
        commitDeferredBubbleVisuals(result.addedCount);
        if (Number(tradeFloor) !== Number(activeTradeQueryMinNotional())) {
          scheduleHistoryForViewport(0, "trades");
          return;
        }
        tradeBudget.bytes += Number(result.bytes) || 0;
        if (result.completed && !result.partial) {
          // 若使用 [floor, upper) 增量带，已有 >=upper 覆盖与本次结果合起来
          // 即构成完整 >=floor 覆盖，因此可直接登记 floor。
          addTradeHistoryLoadedRange(tradeFloor, chunkFromMs, chunkToMs);
          historyLoadStats.completedTradeChunks += 1;
        } else {
          partial = true;
        }
        if (tradeBudget.bytes > HISTORY_MAX_TOTAL_RESPONSE_BYTES) {
          partial = true;
          break;
        }
        chunkToMs = chunkFromMs;
        await yieldHistoryEventLoop(signal);
      }
      if (tradeBudget.bytes > HISTORY_MAX_TOTAL_RESPONSE_BYTES) break;
    }

    const remainingBook = bookToMs > bookFromMs
      ? missingBookHistoryRanges(bookFromMs, bookToMs)
      : [];
    const remainingTrades = missingTradeHistoryRanges(
      activeTradeQueryMinNotional(),
      tradeFromMs,
      tradeToMs
    );
    partial =
      partial ||
      state.tradeHistoryTruncated ||
      remainingBook.length > 0 ||
      remainingTrades.length > 0;
    if (
      loadedColumnCount === 0 &&
      state.historyColumns === 0 &&
      state.bubbles.length === 0
    ) {
      state.historyStatus = partial ? "partial" : "warming";
      showHistoryNotice("复盘历史正在积累，当前冻结画面保持可用");
      return;
    }
    state.historyStatus = partial ? "partial" : "ready";
    if (partial) showHistoryNotice("部分复盘历史不可用；当前冻结画面保持可用");
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (!historyIdentityIsCurrent(identity)) return;
    state.historyStatus = "unavailable";
    showHistoryNotice("复盘历史不可用；当前冻结画面保持可用");
  } finally {
    if (requestSerial === historyRequestSerial) {
      historyController = null;
      historyInFlightRange = null;
      historyInFlightKind = null;
      historyInFlightTradeFloor = null;
      historyScheduleQueued = false;
      const rerun = historyRefreshPending;
      const rerunDelay = historyQueuedDelayMs;
      const rerunScope = historyPendingScope === "trades" ? "trades" : "all";
      historyRefreshPending = false;
      historyPendingScope = null;
      if (rerun && currentHistoryIdentity()) {
        scheduleHistoryForViewport(rerunDelay, rerunScope);
      } else if (needsFrozenTailBackfill && currentHistoryIdentity()) {
        scheduleHistoryForViewport(
          frozenBookHistorySettleDelayMs(identity)
        );
      }
    }
  }
}

function mergePendingHistoryScope(scope = "all") {
  const normalized = scope === "trades" ? "trades" : "all";
  if (historyPendingScope === null || historyPendingScope === "trades") {
    historyPendingScope = normalized;
  }
}

function scheduleHistoryForViewport(
  delayMs = HISTORY_REQUEST_DEBOUNCE_MS,
  scope = "all"
) {
  if (state.appMode !== APP_MODE_REPLAY) return;
  if (!currentHistoryIdentity()) return;
  mergePendingHistoryScope(scope);
  historyQueuedDelayMs = Math.max(0, delayMs);
  if (historyViewportTimer !== null) window.clearTimeout(historyViewportTimer);
  if (historyController) {
    // 复盘缩放/范围变化只合并为“当前请求完成后再跑最新一次”；模式切换与
    // 切币仍由 cancelHistoryLoad 显式中止，避免后台请求叠加。
    historyRefreshPending = true;
    historyScheduleQueued = true;
    return;
  }
  const requestSerial = ++historyRequestSerial;
  historyScheduleQueued = true;
  historyViewportTimer = window.setTimeout(() => {
    historyViewportTimer = null;
    loadHistoryForViewport(requestSerial);
  }, historyQueuedDelayMs);
}

function maybeScheduleViewportHistory() {
  if (state.appMode !== APP_MODE_REPLAY) return;
  // feed/grid 尚未 ready 时保持 idle，后续有效 view 或首列会再次走到这里。
  if (state.historyStatus !== "idle" || historyScheduleQueued) return;
  if (!currentHistoryIdentity()) return;
  scheduleHistoryForViewport(0);
}

function maybeRefreshVisibleHistory() {
  if (state.appMode !== APP_MODE_REPLAY) return;
  const identity = currentHistoryIdentity();
  if (!identity) return;
  const liveCoverageMs = Math.max(0, identity.newestT - identity.liveCutoffT);
  if (
    state.historyStatus === "loading" ||
    state.historyStatus === "partial" ||
    state.historyStatus === "unavailable" ||
    historyScheduleQueued ||
    visibleHistoryLookbackMs() <= liveCoverageMs ||
    Date.now() - historyLastRefreshAt < 5000
  ) {
    return;
  }
  scheduleHistoryForViewport(HISTORY_REQUEST_DEBOUNCE_MS);
}

/* -------------------------------------------------------------- 数据接入 */

function resetStream(epoch, target, symbol) {
  cancelHistoryLoad();
  resetEdgeAutoRecenterState();
  pendingReconnectContinuation = false;
  const nextTarget = normalizedTarget(target) || state.target || DEFAULT_TARGET;
  const nextSymbol = symbol || state.symbol;
  const marketChanged = Boolean(
    (nextTarget && nextTarget !== state.target) ||
      (nextSymbol && nextSymbol !== state.symbol)
  );
  const preserveManualCenter = Boolean(
    !marketChanged && !viewport.follow && viewport.centerBucket !== null
  );
  state.streamEpoch = epoch;
  state.target = nextTarget;
  state.symbol = nextSymbol;
  syncPageUrlState();
  if (marketChanged) {
    state.meta = null;
    state.view = null;
    state.status = null;
    lastTrustedBookBounds = null;
    // 新市场从 5% 上限和一次性自动适配开始；同市场重连保留已锁定
    // 相机与人工缩放，不能每次 snapshot 都跳回默认值。
    viewport.coveragePct = DEFAULT_PRICE_COVERAGE_PCT;
    viewport.autoFitBook = true;
    viewport.follow = true;
    viewport.renderedCoveragePct = DEFAULT_PRICE_COVERAGE_PCT;
    viewport.renderedBookCoveragePct = null;
    viewport.bookOutsideViewport = false;
    viewport.priceScaleMode = "pending";
    el("followToggle").checked = true;
    resetAutoBookViewport(false);
  } else {
    resetAutoBookViewport(true);
  }
  state.columns = [];
  state.bubbles = [];
  bubbleTradeIds.clear();
  resetTradeReservoirState();
  rejectedTradeEvents = 0;
  state.trades = [];
  state.dom = null;
  state.mid = null;
  state.scaleLo = 0;
  state.scaleHi = 0;
  state.bubbleRef = 0;
  state.bubbleSizeRef = 0;
  state.bubbleSizeSamples = [];
  state.bubbleSizeProfile = null;
  state.bubbleSizeProfileAt = 0;
  state.bubbleSizeProfileVersion = 0;
  state.bubbleRevision += 1;
  state.historyColumns = 0;
  state.historyLookbackMs = activeMaxLookbackMs();
  state.heatmapRevision += 1;
  state.domRevision += 1;
  liveBookFresh = false;
  visualClock.newestColumnT = null;
  visualClock.visualTimeMs = null;
  visualClock.frameAt = 0;
  visualClock.arrivedAt = 0;
  if (!preserveManualCenter) {
    viewport.centerBucket = null;
    viewport.centerFrameAt = 0;
  }
  invalidateHeatmapCache();
  invalidateLiveBookCache();
  el("tasList").textContent = "";
  syncDensityControl();
  requestRender();
}

function pushColumn(column) {
  const liveT = Number(column.t);
  column.history = false;
  const liveGrid = gridIdentity(state.view);
  // 仅用于前端视觉连续性门禁。旧列之后即使被重投影到新网格，也保留其
  // 原始 stream/grid 身份，网格切换处不会被“连续显示”跨界抹平。
  column.liveStreamEpoch = Number.isFinite(Number(state.streamEpoch))
    ? Number(state.streamEpoch)
    : null;
  column.liveGridKey = liveGrid
    ? [liveGrid.epoch, liveGrid.anchor, liveGrid.ratio].join(":")
    : "";
  // 实时列在首次入列时冻结绝对价格源。后续 grid epoch/anchor 变化只能从
  // 这份源重新投影，不能拿上一次已 floor 的 bucket 继续级联换算。
  captureColumnProjectionSource(column, gridIdentity(state.view));
  if (
    !state.columns.length ||
    Number(state.columns[state.columns.length - 1].t) <= liveT
  ) {
    state.columns.push(column);
  } else {
    let insertAt = lowerBoundColumnTime(state.columns, liveT);
    // 与原来的稳定 sort 保持一致：同 timestamp 的既有列仍排在新列前面。
    while (
      insertAt < state.columns.length &&
      Number(state.columns[insertAt].t) === liveT
    ) {
      insertAt += 1;
    }
    state.columns.splice(insertAt, 0, column);
  }
  // 即使冷打开竞态中历史先到，也保留它作为复盘底图。实时列只在
  // 绘制层覆盖，不再从状态中删除同秒历史，避免毫秒级调度缺口变成黑缝。
  recordColumnArrival(Number(column.t));
  pruneCombinedColumns(liveT);
  updateScale(column);
  if (!column.gap) {
    // 历史列冻结到达时的显示尺度。后续盘口量级变化只能影响新列，不能把整段
    // 历史重新跨过 black cutoff，避免旧流动性墙随每列成片明灭。
    const bounds = heatScaleBounds();
    column.displayScaleLo = bounds.lo;
    column.displayScaleHi = bounds.hi;
  }
  state.heatmapRevision += 1;
}

function updateScale(column) {
  if (column.gap) return;
  const values = [];
  for (const side of [column.bids, column.asks]) {
    if (isTypedHistoryLevels(side)) {
      for (const value of side.values) values.push(value);
    } else if (isFlatWireLevels(side)) {
      for (let index = 1; index < side.length; index += 2) {
        values.push(side[index]);
      }
    } else {
      for (const key in side) values.push(side[key]);
    }
  }
  if (values.length < 3) return;
  values.sort((a, b) => a - b);
  // 下界取中位偏上，让常态挂单沉到暗色，只有真正的流动性墙才走到亮端。
  const lo = Math.max(1, quantile(values, 0.55));
  const hi = Math.max(lo * 6, quantile(values, 0.985) * 3);
  const alpha = state.scaleHi === 0 ? 1 : 0.08;
  state.scaleLo = state.scaleLo * (1 - alpha) + lo * alpha;
  state.scaleHi = state.scaleHi * (1 - alpha) + hi * alpha;
}

function bubbleSizeSampleFor(bubble) {
  if (bubble.history) return null;
  const notional = Number(bubble.notional);
  const t = bubbleEventTimeMs(bubble);
  if (
    !Number.isFinite(t) ||
    !Number.isFinite(notional) ||
    notional < activeTradeMinNotional()
  ) {
    return null;
  }
  return { t, notional };
}

function appendBubbleSizeSamples(bubbles) {
  let needsSort = false;
  for (const bubble of bubbles) {
    const sample = bubbleSizeSampleFor(bubble);
    if (!sample) continue;
    const previous = state.bubbleSizeSamples[state.bubbleSizeSamples.length - 1];
    if (previous && previous.t > sample.t) needsSort = true;
    state.bubbleSizeSamples.push(sample);
  }
  if (!state.bubbleSizeSamples.length) return;
  if (needsSort) {
    state.bubbleSizeSamples.sort((left, right) => left.t - right.t);
  }
  const newestT = state.bubbleSizeSamples[state.bubbleSizeSamples.length - 1].t;
  const cutoff = newestT - BUBBLE_SIZE_SAMPLE_WINDOW_MS;
  let expired = 0;
  while (
    expired < state.bubbleSizeSamples.length &&
    state.bubbleSizeSamples[expired].t < cutoff
  ) {
    expired += 1;
  }
  if (expired > 0) state.bubbleSizeSamples.splice(0, expired);
  if (state.bubbleSizeSamples.length > BUBBLE_SIZE_SAMPLE_LIMIT) {
    state.bubbleSizeSamples.splice(
      0,
      state.bubbleSizeSamples.length - BUBBLE_SIZE_SAMPLE_LIMIT
    );
  }
}

function updateBubbleSizeProfile(now = Date.now(), force = false) {
  if (
    !force &&
    state.bubbleSizeProfileAt > 0 &&
    now - state.bubbleSizeProfileAt < BUBBLE_SIZE_PROFILE_INTERVAL_MS
  ) {
    return state.bubbleSizeProfile;
  }
  const candidate = bubbleSizeProfileForSamples(state.bubbleSizeSamples);
  if (!candidate) return state.bubbleSizeProfile;
  const current = Number(state.bubbleSizeRef) || 0;
  let nextReference = candidate.reference;
  if (current > 0) {
    const logDistance = Math.log(candidate.reference / current);
    if (Math.abs(logDistance) <= Math.log1p(BUBBLE_SIZE_DEADBAND_RATIO)) {
      nextReference = current;
    } else {
      const elapsed = Math.max(
        BUBBLE_SIZE_PROFILE_INTERVAL_MS,
        now - (state.bubbleSizeProfileAt || now)
      );
      const tau =
        candidate.reference > current
          ? BUBBLE_SIZE_SCALE_UP_TAU_MS
          : BUBBLE_SIZE_SCALE_DOWN_TAU_MS;
      const alpha = 1 - Math.exp(-elapsed / tau);
      nextReference = Math.exp(Math.log(current) + alpha * logDistance);
    }
  }
  state.bubbleSizeRef = Math.max(1, nextReference);
  state.bubbleSizeProfile = Object.assign({}, candidate, {
    reference: state.bubbleSizeRef,
  });
  state.bubbleSizeProfileAt = now;
  state.bubbleSizeProfileVersion += 1;
  // 显隐门槛继续独立使用 p90；只复用未经过滤的原生样本，避免历史分辨率污染。
  state.bubbleRef =
    state.bubbleRef === 0
      ? candidate.q90
      : state.bubbleRef * 0.9 + candidate.q90 * 0.1;
  return state.bubbleSizeProfile;
}

function rawAggTradeKey(target, symbol, id, tradeTime) {
  // Hyperliquid tid 只有与 block_time + coin 组合才全局唯一；Binance 同样
  // 带上时间不会改变同一 aggTrade 的 live/history 去重结果。
  return `${normalizedTarget(target)}:${normalizedHistorySymbol(symbol)}:${Number(tradeTime)}:${String(id)}`;
}

function rejectRawAggTrade(message, strict) {
  rejectedTradeEvents += 1;
  if (strict) throw new Error(message);
  return null;
}

function normalizeRawAggTrade(
  item,
  {
    history = false,
    target = state.target,
    symbol = state.symbol,
    expectedSourceEventType = rawTradeSourceEventType(state.view),
    fromMs = null,
    toMs = null,
    strict = false,
  } = {}
) {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    return rejectRawAggTrade("逐条成交记录格式无效", strict);
  }
  // 这些字段只存在于旧聚合气泡；即使 count=1 也不能让语义含混的旧结构
  // 混入逐条流，更不能把 count>1 的小单合计伪装成一笔大单。
  for (const field of [
    "count",
    "firstTradeTime",
    "lastTradeTime",
    "historyResolutionMs",
    "notional",
  ]) {
    if (Object.prototype.hasOwnProperty.call(item, field)) {
      return rejectRawAggTrade("检测到聚合成交字段，已拒绝载入", strict);
    }
  }
  if (item.id === null || item.id === undefined || String(item.id).trim() === "") {
    return rejectRawAggTrade("逐条成交缺少交易 ID", strict);
  }
  const id = String(item.id);
  const sourceEventType = String(item.sourceEventType || "").trim().toLowerCase();
  const expectedSource = String(expectedSourceEventType || "").trim().toLowerCase();
  if (
    (strict && (!expectedSource || sourceEventType !== expectedSource)) ||
    (sourceEventType && expectedSource && sourceEventType !== expectedSource)
  ) {
    return rejectRawAggTrade("逐条成交来源事件类型不匹配", strict);
  }
  if (
    Object.prototype.hasOwnProperty.call(item, "applicationAggregation") &&
    item.applicationAggregation !== false
  ) {
    return rejectRawAggTrade("检测到应用层成交聚合，已拒绝载入", strict);
  }
  if (
    Object.prototype.hasOwnProperty.call(item, "aggregateId") &&
    String(item.aggregateId) !== id
  ) {
    return rejectRawAggTrade("逐条成交 ID 身份不一致", strict);
  }
  const tradeTime = Number(item.tradeTime);
  const price = Number(item.price);
  const quantity = Number(item.quantity);
  const notionalValue = Number(item.notionalValue);
  const side = String(item.side || "").trim().toLowerCase();
  if (
    !Number.isFinite(tradeTime) ||
    tradeTime <= 0 ||
    !Number.isFinite(price) ||
    price <= 0 ||
    !Number.isFinite(quantity) ||
    quantity <= 0 ||
    !Number.isFinite(notionalValue) ||
    notionalValue <= 0 ||
    (side !== "buy" && side !== "sell")
  ) {
    return rejectRawAggTrade("逐条成交字段无效", strict);
  }
  if (
    (fromMs !== null && Number.isFinite(Number(fromMs)) && tradeTime < Number(fromMs)) ||
    (toMs !== null && Number.isFinite(Number(toMs)) && tradeTime >= Number(toMs))
  ) {
    return rejectRawAggTrade("逐条成交时间超出请求范围", strict);
  }
  const currentGrid = gridIdentity(state.view);
  const bubble = {
    id,
    tradeKey: rawAggTradeKey(target, symbol, id, tradeTime),
    tradeTime,
    t: tradeTime,
    price,
    quantity,
    notionalValue,
    notional: notionalValue,
    side,
    history: Boolean(history),
    sourceKind: rawTradeHistoryKind(state.view) || "live-raw-trades",
    sourceEventType: sourceEventType || expectedSource || "unknown",
    applicationAggregation: false,
    displayEventTimeMs: tradeTime,
  };
  // WebSocket 可能先送到逐条成交、随后第一帧盘口才建立价格网格。实时模式
  // 先以真实成交价和稳定事件 ID 有界保留，网格建立时统一重投影；这不是
  // 上游契约错误，也不能因此丢掉大单。严格复盘仍要求查询前已有稳定网格。
  if (!currentGrid && strict) {
    return rejectRawAggTrade("价格网格尚未就绪", strict);
  }
  if (currentGrid) reprojectBubbleToGrid(bubble, null, currentGrid);
  if (currentGrid && !Number.isFinite(Number(bubble.bucket))) {
    return rejectRawAggTrade("逐条成交价格无法映射到当前网格", strict);
  }
  return bubble;
}

function rebuildBubbleTradeIds() {
  bubbleTradeIds.clear();
  for (const bubble of state.bubbles) {
    if (bubble && bubble.tradeKey) bubbleTradeIds.add(bubble.tradeKey);
  }
}

function removeBubblePrefix(count) {
  const amount = Math.max(0, Math.min(state.bubbles.length, Number(count) || 0));
  if (!amount) return;
  for (let index = 0; index < amount; index += 1) {
    const key = state.bubbles[index] && state.bubbles[index].tradeKey;
    if (key) bubbleTradeIds.delete(key);
  }
  state.bubbles.splice(0, amount);
}

function prepareHigherFloorTradeRecovery(queryFloor, coverageMissing = false) {
  const floor = Number(queryFloor);
  if (
    state.appMode !== APP_MODE_REPLAY ||
    (!state.tradeHistoryTruncated && !coverageMissing) ||
    !Number.isFinite(floor) ||
    floor < archiveMinNotional()
  ) {
    return false;
  }
  // 低门槛发生对象溢出、分页/字节门禁或网络 partial 后，最老区间可能仍有
  // 用户随后要看的大单。提高门槛时先清理已落盘且低于新门槛的隐藏历史，
  // 为补回的大单腾出容量；最近实时 reservoir 与现存大单继续保留。以后降低
  // 门槛再补低额成交，盘口和当前画面都不清空。
  state.bubbles = state.bubbles.filter(
    (bubble) => !bubble.history || Number(bubble.notional) >= floor
  );
  rebuildBubbleTradeIds();
  tradeHistoryCoverage = [];
  state.tradeHistoryTruncated = false;
  state.historyStatus = "loading";
  state.bubbleRevision += 1;
  return true;
}

function pushRawTradeBubbles(
  items,
  {
    history = false,
    target = state.target,
    symbol = state.symbol,
    expectedSourceEventType = rawTradeSourceEventType(state.view),
    fromMs = null,
    toMs = null,
    strict = false,
    deferVisualCommit = false,
  } = {}
) {
  const added = [];
  const pendingIds = new Set();
  const sizeReferenceBeforeArrival = Number(state.bubbleSizeRef) || 0;
  const filterReferenceBeforeArrival = Number(state.bubbleRef) || 0;
  for (const item of Array.isArray(items) ? items : []) {
    const bubble = normalizeRawAggTrade(item, {
      history,
      target,
      symbol,
      expectedSourceEventType,
      fromMs,
      toMs,
      strict,
    });
    if (!bubble) continue;
    if (bubbleTradeIds.has(bubble.tradeKey) || pendingIds.has(bubble.tradeKey)) {
      continue;
    }
    pendingIds.add(bubble.tradeKey);
    added.push(bubble);
  }
  added.sort(
    (left, right) => bubbleEventTimeMs(left) - bubbleEventTimeMs(right)
  );
  // 逐条历史和实时尾段只按 target + symbol + 上游事件 ID 精确去重；同一毫秒、
  // 同一价格的两条不同成交事件必须同时保留，绝不按 1 秒桶删除或相加。
  state.bubbles = mergeSortedBubbles(state.bubbles, added);
  for (const bubble of added) bubbleTradeIds.add(bubble.tradeKey);
  const newestT = state.columns.length
    ? Number(state.columns[state.columns.length - 1].t)
    : Date.now();
  const referenceT =
    state.appMode === APP_MODE_REPLAY && Number.isFinite(state.replayAsOfMs)
      ? state.replayAsOfMs
      : newestT;
  const cutoff = referenceT - activeMaxLookbackMs() - 5000;
  let expired = lowerBoundBubbleBucketTime(state.bubbles, cutoff);
  if (expired >= BUBBLE_PREFIX_COMPACT_THRESHOLD) {
    removeBubblePrefix(expired);
    expired = 0;
  }
  // 硬帽判断前必须强制清完所有过窗前缀，不能把最多 1023 条延迟压缩误报为
  // 有效窗口溢出；只有窗口内真实逐笔超过上限才进入 partial。
  if (state.bubbles.length > MAX_COMBINED_BUBBLES && expired > 0) {
    removeBubblePrefix(expired);
  }
  if (state.bubbles.length > MAX_COMBINED_BUBBLES) {
    const overflow = state.bubbles.length - MAX_COMBINED_BUBBLES;
    removeBubblePrefix(overflow);
    state.tradeHistoryTruncated = true;
    state.tradeHistoryDropped += overflow;
    // 任意时间前缀被淘汰后，既有金额门槛 coverage 都不再能证明完整。
    // 清空成交覆盖即可；盘口覆盖仍然独立有效。
    tradeHistoryCoverage = [];
  }
  const bootstrapSamples = added
    .map((bubble) => bubbleSizeSampleFor(bubble))
    .filter(Boolean);
  const fallbackSamples = bootstrapSamples.length
    ? bootstrapSamples
    : added
        .map((bubble) => ({
          t: bubbleEventTimeMs(bubble),
          notional: Number(bubble.notional),
        }))
        .filter(
          (sample) =>
            Number.isFinite(sample.t) &&
            Number.isFinite(sample.notional) &&
            sample.notional > 0
        );
  const bootstrapProfile = bubbleSizeProfileForSamples(fallbackSamples);
  // 正常实时批次先按“到达前”的画像定型；只有冷启动首批才用整批样本预热。
  const sizeReferenceAtArrival = Math.max(
    1,
    sizeReferenceBeforeArrival ||
      (bootstrapProfile && bootstrapProfile.reference) ||
      BUBBLE_SIZE_COLD_START_NOTIONAL
  );
  const filterReferenceAtArrival = Math.max(
    1,
    filterReferenceBeforeArrival ||
      (bootstrapProfile && bootstrapProfile.q90) ||
      1
  );
  for (const bubble of added) {
    // 强度、尺寸参考量级和最终半径都在到达时冻结；画像更新不让旧气泡整体呼吸。
    const notional = Math.max(0, Number(bubble.notional) || 0);
    const strength = notional / filterReferenceAtArrival;
    const sizeRatio = notional / sizeReferenceAtArrival;
    bubble.displayStrength = strength;
    bubble.displaySizeReference = sizeReferenceAtArrival;
    bubble.displaySizeRatio = sizeRatio;
    bubble.displaySizeProfileVersion = state.bubbleSizeProfileVersion;
    bubble.displayRadiusUnit = bubbleRadiusUnit(
      notional,
      sizeReferenceAtArrival
    );
    bubble.displayEmphasis = strength >= 3 || sizeRatio >= 1;
  }
  appendBubbleSizeSamples(added);
  updateBubbleSizeProfile(Date.now(), state.bubbleSizeRef === 0);
  if (added.length && !deferVisualCommit) state.bubbleRevision += 1;
  if (!deferVisualCommit) syncDensityControl();
  return added.length;
}

function windowMs() {
  return state.view && state.view.heatmapWindowSeconds
    ? state.view.heatmapWindowSeconds * 1000
    : 180000;
}

function messageMatchesCurrentStream(message, incomingTarget) {
  if (!message || !state.columns.length) return false;
  const incomingStreamEpoch = Number(message.streamEpoch);
  const currentStreamEpoch = Number(state.streamEpoch);
  return (
    incomingTarget === state.target &&
    normalizedHistorySymbol(message.symbol) ===
      normalizedHistorySymbol(state.symbol) &&
    Number.isFinite(incomingStreamEpoch) &&
    Number.isFinite(currentStreamEpoch) &&
    incomingStreamEpoch === currentStreamEpoch
  );
}

function resetStartsReconnectContinuation(
  message,
  incomingTarget,
  previousSubscriptionEpoch,
  incomingSubscriptionEpoch
) {
  return Boolean(
    message &&
    message.type === "reset" &&
    previousSubscriptionEpoch < 0 &&
    Number.isFinite(incomingSubscriptionEpoch) &&
    messageMatchesCurrentStream(message, incomingTarget)
  );
}

function snapshotContinuesCurrentStream(
  message,
  incomingTarget,
  previousSubscriptionEpoch,
  incomingSubscriptionEpoch,
  pendingResetContinuation = false
) {
  if (
    !message ||
    message.type !== "snapshot" ||
    !messageMatchesCurrentStream(message, incomingTarget)
  ) {
    return false;
  }
  return Boolean(
    pendingResetContinuation ||
    (previousSubscriptionEpoch < 0 &&
      Number.isFinite(incomingSubscriptionEpoch))
  );
}

function prepareColumnsForSnapshot(snapshotColumns, continuation) {
  if (!continuation) {
    state.columns = [];
    state.scaleLo = 0;
    state.scaleHi = 0;
    return;
  }
  if (!snapshotColumns.length) return;
  const snapshotFromMs = snapshotColumns.reduce((earliest, column) => {
    const timestamp = Number(column && column.t);
    return Number.isFinite(timestamp) ? Math.min(earliest, timestamp) : earliest;
  }, Infinity);
  if (Number.isFinite(snapshotFromMs)) {
    state.columns = state.columns.filter(
      (column) => Number(column.t) < snapshotFromMs
    );
  }
}

function applyMessage(message) {
  if (state.appMode !== APP_MODE_LIVE) return;
  if (message.type === "error") {
    const detail = message.message || "服务端拒绝了本次请求";
    el("gapNotice").textContent = `切换失败：${detail}`;
    el("gapNotice").classList.remove("hidden");
    el("targetSelect").value = state.target || DEFAULT_TARGET;
    el("symbolInput").value = state.symbol || el("symbolInput").value;
    syncSymbolSelection();
    syncTargetPresentation();
    setTimeout(() => el("gapNotice").classList.add("hidden"), 3500);
    return;
  }
  const declaredTarget = normalizedTarget(message.target);
  // 滚动兼容：旧 Binance 进程没有 target 字段时只允许落到默认市场；
  // 非默认市场缺 target 必须 fail closed，不能把同名合约混入当前页面。
  const incomingTarget =
    declaredTarget || (state.target === DEFAULT_TARGET ? DEFAULT_TARGET : "");
  if (!incomingTarget) return;
  const incomingSubscriptionEpoch = Number(message.subscriptionEpoch);
  const previousSubscriptionEpoch = state.subscriptionEpoch;
  const reconnectResetContinuation = resetStartsReconnectContinuation(
    message,
    incomingTarget,
    previousSubscriptionEpoch,
    incomingSubscriptionEpoch
  );
  const reconnectContinuation = snapshotContinuesCurrentStream(
    message,
    incomingTarget,
    previousSubscriptionEpoch,
    incomingSubscriptionEpoch,
    pendingReconnectContinuation
  );
  let streamResetForMessage = false;
  if (Number.isFinite(incomingSubscriptionEpoch)) {
    // 每次 WebSocket 重连会从新的连接内 epoch 重新计数；connect.onopen 会先
    // 把本地 epoch 复位。连接存活期间只接受当前或更新的订阅，拒绝切币前已
    // 离开发送队列的迟到帧。
    if (
      state.subscriptionEpoch >= 0 &&
      incomingSubscriptionEpoch < state.subscriptionEpoch
    ) {
      return;
    }
    state.subscriptionEpoch = incomingSubscriptionEpoch;
    // reset/snapshot 属于服务端的原子控制对；若极端断线恢复只留下后者或新
    // epoch 的首帧，仍以 epoch + symbol 主动切断旧页面状态，不能永久卡在旧币。
    if (
      incomingSubscriptionEpoch > previousSubscriptionEpoch &&
      message.type !== "reset" &&
      !reconnectContinuation &&
      incomingTarget &&
      message.symbol
    ) {
      resetStream(message.streamEpoch, incomingTarget, message.symbol);
      streamResetForMessage = true;
    }
  }
  if (message.type === "reset") {
    if (reconnectResetContinuation) {
      // 新 WebSocket 的现役协议控制对固定为 reset → snapshot。同市场、同
      // stream 的首个 reset 只是重订阅门牌，不能提前清掉浏览器10分钟前缀；
      // 只允许紧随其后的匹配 snapshot 消费这次续接资格。
      pendingReconnectContinuation = true;
      return;
    }
    pendingReconnectContinuation = false;
    resetStream(message.streamEpoch, incomingTarget, message.symbol);
    el("gapNotice").textContent = message.reason || "数据流已重置";
    el("gapNotice").classList.remove("hidden");
    setTimeout(() => el("gapNotice").classList.add("hidden"), 2500);
    return;
  }
  if (
    ((incomingTarget && state.target && incomingTarget !== state.target) ||
      (message.symbol && state.symbol && message.symbol !== state.symbol))
  ) {
    return;
  }
  if (message.streamEpoch !== undefined && message.streamEpoch !== state.streamEpoch) {
    resetStream(message.streamEpoch, incomingTarget, message.symbol);
    streamResetForMessage = true;
  }
  state.target = incomingTarget;
  if (message.symbol) state.symbol = message.symbol;
  if (message.meta) state.meta = message.meta;
  const columnCadenceChanged = message.view
    ? applyViewUpdate(message.view)
    : false;
  if (message.status) state.status = message.status;
  if (message.status && message.status.overall !== "LIVE") {
    liveBookFresh = false;
  }
  if (message.dom) {
    state.dom = message.dom;
    state.domRevision += 1;
    invalidateLiveBookCache();
    liveBookFresh =
      transportConnected &&
      !!message.dom.ready &&
      !!message.status &&
      message.status.overall === "LIVE";
  }
  if (message.mid !== undefined && message.mid !== null) state.mid = message.mid;
  if (message.lastTrade) state.lastTrade = message.lastTrade;
  if (message.droppedTrades !== undefined) {
    state.droppedTrades = message.droppedTrades;
  }

  if (message.type === "snapshot") {
    // 同市场、同 stream 的重连快照只有服务端短 ring。保留快照起点以前的
    // 浏览器会话前缀，只原子替换重叠尾段；非历史名单币才能维持完整10分钟。
    // 真正切币/stream 换代已由 resetStream 清空，仍从新快照重建。
    cancelHistoryLoad();
    const snapshotColumns = Array.isArray(message.columns) ? message.columns : [];
    prepareColumnsForSnapshot(
      snapshotColumns,
      reconnectContinuation && !streamResetForMessage && !columnCadenceChanged
    );
    for (const column of snapshotColumns) pushColumn(column);
    // 快照批量入列不能把视觉时钟留在第一列；最后统一锚到快照最新列。
    anchorVisualClockToNewest();
    if (streamResetForMessage || !reconnectContinuation) {
      state.bubbles = [];
      bubbleTradeIds.clear();
      resetTradeReservoirState();
      state.bubbleSizeSamples = [];
      state.bubbleSizeProfile = null;
      state.bubbleSizeProfileAt = 0;
    }
    // 新版 bubbles 是不受 TAS 每帧 200 条上限影响的完整逐事件通道，优先入列；
    // trades 只作即时兼容补漏。同一 ID 会被精确去重，旧聚合 bubbles 因含
    // count/notional/firstTradeTime 等字段会被拒绝，绝不回退为聚合语义。
    pushRawTradeBubbles(message.bubbles || [], { history: false });
    pushRawTradeBubbles(message.trades || [], { history: false });
    appendTrades(message.trades || [], true);
    hasFirstFrame = state.columns.length > 0;
    syncModeControls();
    if (message.symbol) {
      el("targetSelect").value = state.target;
      el("symbolInput").value = message.symbol;
      syncSymbolSelection();
      savePreference("target", state.target);
      savePreference(symbolPreferenceKey(state.target), message.symbol);
      syncTargetPresentation();
      // snapshot 是服务端确认市场身份的时点。若交易所返回了规范大小写，
      // 地址也在这里收敛到可复制的 canonical target+symbol。
      syncPageUrlState();
    }
    pendingReconnectContinuation = false;
  } else {
    if (message.column) pushColumn(message.column);
    if (message.bubbles && message.bubbles.length) {
      pushRawTradeBubbles(message.bubbles, { history: false });
    }
    if (message.trades && message.trades.length) {
      pushRawTradeBubbles(message.trades, { history: false });
      appendTrades(message.trades, false);
    }
  }
  // 新建 Hub 的首个 snapshot 可能尚无热图列。复盘入口必须在随后第一根
  // 增量列到达时解锁，不能只依赖 snapshot 当刻是否已有缓存。
  if (!hasFirstFrame && state.columns.length > 0) {
    hasFirstFrame = true;
    syncModeControls();
  }
  if (maybeEnterInitialReplayMode()) return;
  // 实时盯盘不读取磁盘历史；复盘历史只由显式模式入口调度。
  updateHeader();
  requestRender();
}

function connect() {
  if (!liveConnectionWanted || state.appMode !== APP_MODE_LIVE) return;
  if (
    socket &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const requestedTarget =
    normalizedTarget(state.target || el("targetSelect").value) || DEFAULT_TARGET;
  const requestedSymbol =
    state.symbol ||
    canonicalSymbolForTarget(requestedTarget, el("symbolInput").value) ||
    "BTCUSDT";
  const generation = ++socketGeneration;
  const connection = new WebSocket(
    `${proto}://${location.host}${appPath("/ws")}?target=${encodeURIComponent(
      requestedTarget
    )}&symbol=${encodeURIComponent(
      requestedSymbol
    )}&wire=${encodeURIComponent(WIRE_PROTOCOL)}&minNotional=${encodeURIComponent(
      String(activeTradeMinNotional())
    )}`
  );
  socket = connection;
  connection.onopen = () => {
    if (generation !== socketGeneration || state.appMode !== APP_MODE_LIVE) {
      connection.close(1000, "mode changed");
      return;
    }
    reconnectDelay = 500;
    pendingReconnectContinuation = false;
    state.subscriptionEpoch = -1;
    transportConnected = true;
    liveBookFresh = false;
    invalidateLiveBookCache();
    requestRender();
    setBadge("CONNECTED", "waiting");
  };
  connection.onmessage = (event) => {
    if (generation !== socketGeneration || state.appMode !== APP_MODE_LIVE) return;
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    applyMessage(message);
  };
  connection.onclose = () => {
    if (socket === connection) socket = null;
    if (
      generation !== socketGeneration ||
      !liveConnectionWanted ||
      state.appMode !== APP_MODE_LIVE
    ) {
      return;
    }
    transportConnected = false;
    liveBookFresh = false;
    invalidateLiveBookCache();
    setBadge("DISCONNECTED", "bad");
    el("feedStatus").textContent = "与本机服务断开，正在重连";
    requestRender();
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(8000, reconnectDelay * 2);
  };
  connection.onerror = () => connection.close();
}

/* ------------------------------------------------------------------ 顶栏 */

function setBadge(text, kind) {
  const badge = el("statusBadge");
  badge.textContent = text;
  badge.className = `badge ${kind}`;
}

function updateHeader() {
  const status = state.status;
  if (state.appMode === APP_MODE_REPLAY) {
    setBadge("REPLAY", "waiting");
    const asOf = Number(state.replayAsOfMs);
    const eventType = rawTradeSourceEventType(state.view) || "trade";
    el("feedStatus").textContent = Number.isFinite(asOf)
      ? `${targetLabel(state.target)} · 静态复盘 · ${fmtClock(asOf)} 截止 · 逐条 ${eventType} · 已冻结`
      : `${targetLabel(state.target)} · 静态复盘 · 逐条 ${eventType} · 已冻结`;
    el("lastValue").textContent = "--";
    el("midValue").textContent = "--";
    el("spreadValue").textContent = "--";
    el("gridStatus").textContent = state.replaySessionLimited
      ? "无可用逐条成交历史 · 仅冻结当前会话 · 最多10分钟"
      : `磁盘复盘 · 固定${Math.round(activeMaxLookbackMs() / 60000)}分钟 · ${tradeFilterLabel()}` +
        (activeTradeMinNotional() < activeTradeQueryMinNotional()
          ? `（归档≥${fmtCompact(activeTradeQueryMinNotional())}）`
          : "");
  } else if (status) {
    const overall = status.overall || "--";
    const kind =
      overall === "LIVE" ? "live" : overall === "DEGRADED" ? "waiting" : overall === "STALE" ? "bad" : "waiting";
    setBadge(overall, kind);
    const reason = String(status.depthReason || "");
    const windowShift =
      /snapshot|边界|可信窗|完整深度/i.test(reason) &&
      (overall === "RECONNECTING" || overall === "SYNCING" || overall === "CONNECTING");
    el("feedStatus").textContent =
      `depth ${status.depth} · trade ${status.trade} · resync ${status.resyncCount} · ` +
      `重连 ${status.depthReconnects}/${status.tradeReconnects}` +
      (windowShift ? " · 可信窗重居中中" : "");
  }
  if (state.appMode === APP_MODE_LIVE) {
    el("lastValue").textContent = state.lastTrade
      ? fmtPrice(state.lastTrade.price)
      : "--";
    el("lastValue").style.color = state.lastTrade
      ? state.lastTrade.side === "buy"
        ? "#86efac"
        : "#fca5a5"
      : "";
    el("midValue").textContent = state.mid ? fmtPrice(state.mid) : "--";
  } else {
    el("lastValue").style.color = "";
  }

  const dom = state.dom;
  const domMetrics = currentDomMetrics();
  if (
    state.appMode === APP_MODE_LIVE &&
    dom &&
    dom.ready &&
    domMetrics.bidCount &&
    domMetrics.askCount
  ) {
    const bestBid = domMetrics.bestBidUpper;
    const bestAsk = domMetrics.bestAskLower;
    el("spreadValue").textContent = fmtPrice(bestAsk - bestBid);
    const totalBid = domMetrics.totalBid;
    const totalAsk = domMetrics.totalAsk;
    const sum = totalBid + totalAsk;
    const bidPct = sum > 0 ? (totalBid / sum) * 100 : 0;
    const askPct = sum > 0 ? (totalAsk / sum) * 100 : 0;
    const depthLimit = dom.snapshotDepthLimit || 1000;
    const bidCov = Number(dom.trustedBidCoveragePct);
    const askCov = Number(dom.trustedAskCoveragePct);
    const thin =
      Number.isFinite(bidCov) &&
      Number.isFinite(askCov) &&
      bidCov + askCov < 1.5;
    const isHyperliquid = state.target === "hyperliquid_perp";
    el("gridStatus").textContent =
      `${isHyperliquid ? "可见盘口" : "深度"} ${depthLimit}档` +
      (isHyperliquid ? " · 边界外未知" : "") +
      ` · 可信覆盖 bid ${bidCov.toFixed(2)}% / ask ${askCov.toFixed(2)}%` +
      ` · 买/卖 ${bidPct.toFixed(0)}%/${askPct.toFixed(0)}%` +
      (thin && !isHyperliquid ? " · 窗口较浅会更常重拉" : "");
  }

  const covered = state.columns.length
    ? (state.columns[state.columns.length - 1].t - state.columns[0].t) / 1000
    : 0;
  el("perfStatus").textContent =
    `热图 ${covered.toFixed(0)}s · 列 ${state.columns.length}` +
    (state.historyColumns ? `（历史 ${state.historyColumns}）` : "") +
    ` · 逐条气泡 ${state.bubbles.length} · ${tradeFilterLabel()}` +
    ` · 视觉 ${frameStats.fps.toFixed(0)}/${visualFrameLabel()}` +
    ` · 逐条丢弃 ${state.droppedTrades} · 契约拒绝 ${rejectedTradeEvents}`;
  syncHeatContrastControl(
    pendingHeatContrast === null ? ui.heatContrast : pendingHeatContrast
  );
  // verify 诊断由页面底部的 1Hz 定时器统一维护；行情帧不能反复扫描并
  // JSON 序列化整段历史，否则验收模式本身会制造长时间卡顿。
  el("symbolInput").placeholder = state.symbol;
}

/* ------------------------------------------------------------------- TAS */

function rememberTrades(trades, replace) {
  if (replace) state.trades = [];
  state.trades.push(...trades);
  if (state.trades.length > MAX_TAS_BUFFER) {
    state.trades.splice(0, state.trades.length - MAX_TAS_BUFFER);
  }
}

function tasRow(trade) {
  const row = document.createElement("div");
  row.className = `tas-row ${trade.side}`;
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = fmtClock(trade.tradeTime);
  const price = document.createElement("span");
  price.className = "price";
  price.textContent = fmtPrice(Number(trade.price));
  const amount = document.createElement("span");
  amount.className = "amount";
  amount.textContent = fmtNotional(trade.notionalValue);
  row.append(time, price, amount);
  return row;
}

function renderTas() {
  const list = el("tasList");
  list.textContent = "";
  const fragment = document.createDocumentFragment();
  let shown = 0;
  for (let i = state.trades.length - 1; i >= 0 && shown < MAX_TAS_ROWS; i -= 1) {
    const trade = state.trades[i];
    if (trade.notionalValue < ui.tasThreshold) continue;
    fragment.append(tasRow(trade));
    shown += 1;
  }
  list.append(fragment);
}

function appendTrades(trades, replace) {
  rememberTrades(trades, replace);
  const list = el("tasList");
  if (ui.paused) return;
  if (replace) {
    renderTas();
    return;
  }
  const fragment = document.createDocumentFragment();
  for (let i = trades.length - 1; i >= 0; i -= 1) {
    const trade = trades[i];
    if (trade.notionalValue < ui.tasThreshold) continue;
    fragment.append(tasRow(trade));
  }
  if (!fragment.childElementCount) return;
  list.prepend(fragment);
  while (list.childElementCount > MAX_TAS_ROWS) list.lastElementChild.remove();
}

/* ---------------------------------------------------------------- 渲染层 */

/*
 * 三套配色对标 Quantower 的 Monochrome / Bichrome / Heatmap：
 *
 * - monochrome：热图退成灰阶，把红绿完全让给成交气泡和买卖线，气泡最醒目；
 * - bichrome  ：买挂青绿、卖挂琥珀，方向一眼可分；卖挂用琥珀而非纯红，
 *               是为了和"主动卖"的纯红气泡拉开距离；
 * - heatmap   ：经典热力色阶，强度优先，找厚墙最快，但气泡需要靠描边托出。
 */
const PALETTES = {
  monochrome: {
    split: false,
    unified: [
      [0.0, 12, 18, 26],
      [0.35, 54, 68, 82],
      [0.68, 142, 158, 172],
      [1.0, 255, 255, 255],
    ],
  },
  bichrome: {
    split: true,
    bid: [
      [0.0, 6, 28, 28],
      [0.35, 11, 78, 72],
      [0.7, 26, 160, 138],
      [1.0, 150, 245, 224],
    ],
    ask: [
      [0.0, 34, 18, 10],
      [0.35, 112, 52, 20],
      [0.7, 198, 98, 34],
      [1.0, 255, 198, 142],
    ],
  },
  heatmap: {
    split: false,
    // 刻意跳过绿色：绿要留给主动买气泡，热力段走 深青 → 钢蓝 → 黄 → 橙 → 红。
    unified: [
      [0.0, 8, 26, 36],
      [0.3, 22, 78, 106],
      [0.55, 74, 132, 168],
      [0.74, 232, 216, 74],
      [0.89, 246, 146, 40],
      [1.0, 255, 66, 56],
    ],
  },
};

const TAU = Math.PI * 2;
const HEAT_STEPS = 96;
const heatLut = new Map();

function activePalette() {
  return PALETTES[ui.colorMode] || PALETTES.bichrome;
}

function paletteStops(palette, side) {
  if (!palette.split) return palette.unified;
  return side === "ask" ? palette.ask : palette.bid;
}

function rampColor(stops, t) {
  for (let i = 1; i < stops.length; i += 1) {
    if (t <= stops[i][0]) {
      const a = stops[i - 1];
      const b = stops[i];
      const k = (t - a[0]) / (b[0] - a[0] || 1);
      return [
        Math.round(a[1] + (b[1] - a[1]) * k),
        Math.round(a[2] + (b[2] - a[2]) * k),
        Math.round(a[3] + (b[3] - a[3]) * k),
      ];
    }
  }
  const last = stops[stops.length - 1];
  return [last[1], last[2], last[3]];
}

function heatRamp(side) {
  // 每帧上万个单元格，颜色走查表，避免重复插值和字符串分配。
  const palette = activePalette();
  const key = `${ui.colorMode}:${palette.split ? side : "u"}`;
  let ramp = heatLut.get(key);
  if (ramp) return ramp;
  const stops = paletteStops(palette, side);
  ramp = new Array(HEAT_STEPS + 1);
  for (let i = 0; i <= HEAT_STEPS; i += 1) {
    const rgb = rampColor(stops, i / HEAT_STEPS);
    ramp[i] = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
  }
  heatLut.set(key, ramp);
  return ramp;
}

function heatContrastCutoffT(value = ui.heatContrast) {
  return Math.max(0, Math.min(0.95, Number(value) / 100 || 0));
}

function heatScaleBounds(scaleLo = state.scaleLo, scaleHi = state.scaleHi) {
  const lo = Math.max(1, Number(scaleLo) || 0);
  const hi = Math.max(lo * 2, Number(scaleHi) || 0);
  return { lo, hi };
}

function heatCutoffValue() {
  const { lo, hi } = heatScaleBounds();
  return lo * Math.pow(hi / lo, heatContrastCutoffT());
}

function heatColor(value, side, bounds = heatScaleBounds()) {
  const { lo, hi } = bounds;
  let t = Math.log(Math.max(value, lo) / lo) / Math.log(hi / lo);
  t = t > 1 ? 1 : t < 0 ? 0 : t;
  const cutoff = heatContrastCutoffT();
  if (cutoff > 0) {
    if (t <= cutoff) return null;
    // Bookmap black cut-off 的语义等价实现：低强度沉入黑底，剩余区间重新铺满色带。
    t = (t - cutoff) / (1 - cutoff);
  }
  return heatRamp(side)[(t * HEAT_STEPS) | 0];
}

function gradientCss(stops) {
  const cutoff = heatContrastCutoffT();
  const parts = [];
  if (cutoff > 0) {
    const cutoffPct = (cutoff * 100).toFixed(1);
    parts.push(`rgb(4,7,11) 0%`, `rgb(4,7,11) ${cutoffPct}%`);
  }
  for (const stop of stops) {
    const position = cutoff + stop[0] * (1 - cutoff);
    parts.push(
      `rgb(${stop[1]},${stop[2]},${stop[3]}) ${(
        position * 100
      ).toFixed(1)}%`
    );
  }
  return `linear-gradient(90deg, ${parts.join(", ")})`;
}

function bookCoverageBounds() {
  // 可信 DOM 的当前观测边界。它只用于首次建立锁定相机与判断
  // “盘口是否超出视窗”，绝不能在每个渲染帧直接改变几何。
  const dom = state.dom;
  const marketKey = `${state.target}\u001f${state.symbol}`;
  let lo = null;
  let hi = null;
  let levelCount = 0;
  let fresh = false;
  let domRevision = -1;
  if (dom && dom.ready) {
    const metrics = currentDomMetrics();
    lo = metrics.trustedLowerPrice;
    hi = metrics.trustedUpperPrice;
    levelCount = metrics.trustedLevelCount;
    if (lo !== null && hi !== null && lo > 0 && hi > lo) {
      fresh = true;
      domRevision = state.domRevision;
      lastTrustedBookBounds = {
        marketKey,
        lowerPrice: lo,
        upperPrice: hi,
        levelCount,
        domRevision,
      };
    }
  }
  // 当前 DOM 短暂 ready=false 时只冻结纵轴边界，绝不把旧 DOM 重新画成
  // 当前盘口。这样薄盘口仍保持真实覆盖，不会让持久的 5% 用户目标闪回。
  if (
    (lo === null || hi === null || lo <= 0 || hi <= lo) &&
    state.appMode === APP_MODE_LIVE &&
    lastTrustedBookBounds &&
    lastTrustedBookBounds.marketKey === marketKey
  ) {
    lo = lastTrustedBookBounds.lowerPrice;
    hi = lastTrustedBookBounds.upperPrice;
    levelCount = lastTrustedBookBounds.levelCount;
    domRevision = lastTrustedBookBounds.domRevision;
  }
  if (lo === null || hi === null || lo <= 0 || hi <= lo) return null;
  const lowerBucket = priceToBucket(lo);
  const upperBucket = priceToBucket(hi);
  if (
    lowerBucket === null ||
    upperBucket === null ||
    !Number.isFinite(lowerBucket) ||
    !Number.isFinite(upperBucket) ||
    upperBucket <= lowerBucket
  ) {
    return null;
  }
  return {
    marketKey,
    lowerPrice: lo,
    upperPrice: hi,
    lowerBucket,
    upperBucket,
    coveragePct: (hi / lo - 1) * 100,
    levelCount,
    fresh,
    domRevision,
  };
}

function bookCoverageLimit() {
  const bounds = bookCoverageBounds();
  if (bounds) return bounds.coveragePct;
  // 边界字段异常时回退到服务端可信覆盖；只用于诊断展示，
  // 不再钳制相机，也不据此虚构档位。
  const dom = state.dom;
  if (!dom || !dom.ready) return null;
  const bid = Number(dom.trustedBidCoveragePct);
  const ask = Number(dom.trustedAskCoveragePct);
  if (Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask > 0) {
    const lowFactor = Math.max(0.01, 1 - bid / 100);
    const highFactor = 1 + ask / 100;
    return (highFactor / lowFactor - 1) * 100;
  }
  return null;
}

function coverageToRowHeight(plotBottom, coveragePct) {
  const gridRatio = Number(state.view && state.view.gridRatio);
  const target = Number(coveragePct);
  if (
    !Number.isFinite(gridRatio) ||
    gridRatio <= 1 ||
    !Number.isFinite(target) ||
    target <= 0
  ) {
    return Math.max(1, viewport.rowHeight);
  }
  // 连续 bucket 跨度，不再强制至少一档或把行高封顶 48px。粗网格下目标
  // 可能小于一档；离散化只决定画哪些 cell，不能反过来篡改用户目标。
  const span = Math.log1p(target / 100) / Math.log(gridRatio);
  if (!Number.isFinite(span) || span <= 0) return Math.max(1, viewport.rowHeight);
  return Math.max(0.0001, plotBottom / span);
}

function clampCoverageTarget(pct) {
  const numeric = Number(pct);
  const candidate = Number.isFinite(numeric) && numeric > 0
    ? numeric
    : DEFAULT_PRICE_COVERAGE_PCT;
  // 用户目标只受产品硬边界限制。若再用每帧盘口覆盖钳制，目标本身
  // 就会随最外档呼吸，即使 render 不回写 viewport 也会造成画面跳动。
  return Math.max(
    MIN_PRICE_COVERAGE_PCT,
    Math.min(MAX_PRICE_COVERAGE_PCT, candidate)
  );
}

function nextLockedBookViewportState(previous, observation) {
  const prior = previous && typeof previous === "object" ? previous : {};
  const marketKey = String(observation && observation.marketKey || "");
  const requiredLogHalfSpan = Number(
    observation && observation.requiredLogHalfSpan
  );
  const targetCoveragePct = Number(
    observation && observation.targetCoveragePct
  );
  const rawBookCoveragePct = Number(
    observation && observation.rawBookCoveragePct
  );
  const gridLog = Number(observation && observation.gridLog);
  const domRevision = Number(observation && observation.domRevision);
  const forceRefit = Boolean(observation && observation.forceRefit);
  const targetLogHalfSpan = Math.log1p(targetCoveragePct / 100) / 2;
  if (
    !marketKey ||
    !Number.isFinite(requiredLogHalfSpan) ||
    requiredLogHalfSpan <= 0 ||
    !Number.isFinite(targetLogHalfSpan) ||
    targetLogHalfSpan <= 0 ||
    !Number.isFinite(rawBookCoveragePct) ||
    rawBookCoveragePct <= 0 ||
    !Number.isFinite(gridLog) ||
    gridLog <= 0 ||
    !Number.isFinite(domRevision)
  ) {
    return prior;
  }

  const hasLockedScale = Boolean(
    prior.marketKey === marketKey &&
      Number.isFinite(Number(prior.logHalfSpan)) &&
      Number(prior.logHalfSpan) > 0
  );
  if (hasLockedScale && !forceRefit) {
    // 只更新诊断观测，绝不改 logHalfSpan/fitKind/scaleRevision。
    // 因此外档变动无论持续多久，都不会让纵轴自动呼吸。
    if (Number(prior.lastObservedDomRevision) === domRevision) return prior;
    return Object.assign({}, prior, {
      lastObservedDomRevision: domRevision,
      lastObservedRequiredLogHalfSpan: requiredLogHalfSpan,
    });
  }

  const fitThinBook = rawBookCoveragePct < targetCoveragePct * (1 - 1e-9);
  const paddedBookHalfSpan = Math.max(
    requiredLogHalfSpan / BOOK_AUTO_FIT_INNER_RATIO,
    requiredLogHalfSpan + 2 * gridLog
  );
  const minimumLogHalfSpan =
    Math.log1p(MIN_PRICE_COVERAGE_PCT / 100) / 2;
  const maximumLogHalfSpan =
    Math.log1p(MAX_PRICE_COVERAGE_PCT / 100) / 2;
  // 原始目标只决定“盘口足够深时看多少”。当总盘口不足目标时，
  // 必须优先保证以 mid 为中心的两侧档位全可见；非对称盘口加安全区后
  // 可能略超过 5%，但仍受全局 80% 硬上限保护。
  const logHalfSpan = fitThinBook
    ? Math.max(
        minimumLogHalfSpan,
        Math.min(maximumLogHalfSpan, paddedBookHalfSpan)
      )
    : targetLogHalfSpan;
  return {
    marketKey,
    logHalfSpan,
    fitKind: fitThinBook ? "book" : "target",
    sourceDomRevision: domRevision,
    lastObservedDomRevision: domRevision,
    lastObservedRequiredLogHalfSpan: requiredLogHalfSpan,
    scaleRevision: Math.max(0, Number(prior.scaleRevision) || 0) + 1,
    refitPending: false,
  };
}

function priceViewportGeometry(plotBottom, center, requestedPct = viewport.coveragePct) {
  const coveragePct = clampCoverageTarget(requestedPct);
  const bounds = bookCoverageBounds();
  const marketKey = `${state.target}\u001f${state.symbol}`;
  const gridRatio = Number(state.view && state.view.gridRatio);
  const gridLog = Math.log(gridRatio);
  const centerPrice = bucketToPrice(center);
  let requiredLogHalfSpan = null;
  if (
    bounds &&
    Number.isFinite(centerPrice) &&
    centerPrice > 0 &&
    bounds.lowerPrice > 0 &&
    bounds.upperPrice > bounds.lowerPrice
  ) {
    requiredLogHalfSpan = Math.max(
      Math.abs(Math.log(bounds.lowerPrice / centerPrice)),
      Math.abs(Math.log(bounds.upperPrice / centerPrice))
    );
  }

  if (
    state.appMode === APP_MODE_LIVE &&
    viewport.autoFitBook &&
    bounds &&
    bounds.fresh &&
    Number.isFinite(requiredLogHalfSpan) &&
    requiredLogHalfSpan > 0 &&
    Number.isFinite(gridLog) &&
    gridLog > 0
  ) {
    Object.assign(
      autoBookViewport,
      nextLockedBookViewportState(autoBookViewport, {
        marketKey,
        requiredLogHalfSpan,
        targetCoveragePct: coveragePct,
        rawBookCoveragePct: bounds.coveragePct,
        gridLog,
        domRevision: bounds.domRevision,
        forceRefit: autoBookViewport.refitPending,
      })
    );
  }

  const hasLockedBookViewport = Boolean(
    state.appMode === APP_MODE_LIVE &&
      viewport.autoFitBook &&
      autoBookViewport.marketKey === marketKey &&
      Number.isFinite(autoBookViewport.logHalfSpan) &&
      autoBookViewport.logHalfSpan > 0 &&
      Number.isFinite(gridLog) &&
      gridLog > 0
  );
  if (hasLockedBookViewport) {
    const bucketSpan = Math.max(
      1e-9,
      (2 * autoBookViewport.logHalfSpan) / gridLog
    );
    const allBookVisible = Boolean(
      Number.isFinite(requiredLogHalfSpan) &&
      requiredLogHalfSpan <= autoBookViewport.logHalfSpan * (1 + 1e-9)
    );
    const bookFit = autoBookViewport.fitKind === "book";
    const refitPending = autoBookViewport.refitPending;
    return {
      center,
      rowHeight: Math.max(0.0001, plotBottom / bucketSpan),
      coveragePct: Math.expm1(2 * autoBookViewport.logHalfSpan) * 100,
      atBookEdge: Boolean(bookFit && allBookVisible),
      allBookVisible,
      needsBookRefit: Boolean(bookFit && requiredLogHalfSpan !== null && !allBookVisible),
      rawBookCoveragePct: bounds ? bounds.coveragePct : null,
      fitMode: refitPending
        ? "refit-pending"
        : bookFit
        ? "book-locked"
        : "target-locked",
      levelCount: bounds ? bounds.levelCount : null,
    };
  }
  return {
    center,
    rowHeight: coverageToRowHeight(plotBottom, coveragePct),
    coveragePct,
    atBookEdge: false,
    allBookVisible: null,
    needsBookRefit: false,
    rawBookCoveragePct: bounds ? bounds.coveragePct : null,
    fitMode: viewport.autoFitBook ? "pending" : "manual",
    levelCount: null,
  };
}

function updateCoverageDisplay(geometry, chartWidth) {
  const shown = Number.isFinite(geometry && geometry.coveragePct)
    ? geometry.coveragePct
    : viewport.coveragePct;
  const rawBook = Number(geometry && geometry.rawBookCoveragePct);
  const hasRawBook = Number.isFinite(rawBook) && rawBook > 0;
  const hint = el("coverageHint");
  // render() 已读取过一次画布尺寸；复用该值，避免每帧再次触发布局测量。
  const compact = chartWidth <= COMPACT_CHART_MAX_WIDTH;
  const fitted = geometry && geometry.atBookEdge
    ? compact
      ? " · 全可见"
      : "（全部可见，比例已锁定）"
    : "";
  const outside = geometry && geometry.needsBookRefit
    ? compact
      ? " · 盘口超出 · 双点价轴适配"
      : "（盘口已超出视窗，双击价轴重新适配）"
    : "";
  const pending = geometry && geometry.fitMode === "refit-pending"
    ? compact
      ? " · 等待盘口后适配"
      : "（当前比例已保留，等待有效盘口后适配）"
    : "";
  const touchGuide = window.matchMedia?.("(any-pointer: coarse)")?.matches
    ? compact
      ? " · ↕右侧拖动缩放"
      : " · ↕拖动右侧价轴缩放"
    : "";
  const nextText = hasRawBook
    ? `${compact ? "视窗" : "纵轴视窗"} ${shown.toFixed(2)}% · ${
        compact ? "盘" : "盘口"
      } ${rawBook.toFixed(2)}%${fitted}${outside}${pending}${touchGuide}`
    : `${compact ? "纵轴" : "纵轴覆盖"} ${shown.toFixed(
        2
      )}%${fitted}${outside}${pending}${touchGuide}`;
  if (nextText !== coverageDisplayKey) {
    coverageDisplayKey = nextText;
    hint.textContent = nextText;
    el("coverageValue").textContent = `${shown.toFixed(2)}%`;
  }
  hint.classList.remove("hidden");
}

function syncLegend() {
  const palette = activePalette();
  const split = !!palette.split;
  el("legendHeat").classList.toggle("hidden", split);
  el("legendBid").classList.toggle("hidden", !split);
  el("legendAsk").classList.toggle("hidden", !split);
  // 图例色带直接由当前配色生成，改配色不会和主图对不上。
  if (split) {
    el("legendBidSwatch").style.background = gradientCss(palette.bid);
    el("legendAskSwatch").style.background = gradientCss(palette.ask);
  } else {
    el("legendHeatSwatch").style.background = gradientCss(palette.unified);
  }
}

function normalizeHeatContrast(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? Math.round(Math.max(0, Math.min(95, numeric)))
    : 25;
}

function syncHeatContrastControl(value = ui.heatContrast) {
  const normalized = normalizeHeatContrast(value);
  const input = el("heatContrast");
  input.value = String(normalized);
  el("heatContrastValue").textContent = `${normalized}%`;
  const label = input.closest("label");
  if (label) {
    label.title =
      `Bookmap 风格黑色截止 ${normalized}%：向右会隐藏更多较小挂单，使大挂单更突出；` +
      `当前暗色截止约 ${fmtNotional(heatCutoffValue())}。只改变显示，不改变订单簿数据、` +
      `气泡或 DOM 数值。建议 25%=平衡、60%=突出大单、80%+=极强过滤。`;
  }
}

function applyHeatContrast(value, persist = false) {
  ui.heatContrast = normalizeHeatContrast(value);
  markUiRevision();
  pendingHeatContrast = null;
  syncHeatContrastControl();
  syncLegend();
  invalidateHeatmapCache();
  invalidateLiveBookCache();
  if (persist) savePreference("heatContrast", ui.heatContrast);
  requestRender();
}

function scheduleHeatContrast(value) {
  pendingHeatContrast = normalizeHeatContrast(value);
  // 数字和原生滑块即时跟手，昂贵的历史位图最多约每 70ms 重建一次。
  el("heatContrastValue").textContent = `${pendingHeatContrast}%`;
  if (contrastRenderTimer !== null) return;
  contrastRenderTimer = window.setTimeout(() => {
    contrastRenderTimer = null;
    if (pendingHeatContrast !== null) {
      applyHeatContrast(pendingHeatContrast, false);
    }
  }, 70);
}

function resizeCanvas() {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  viewport.colWidth = Math.max(viewport.colWidth, minTimeColumnWidth());
  markUiRevision();
  invalidateHeatmapCache();
  invalidateLiveBookCache();
  requestRender();
  if (currentHistoryIdentity()) {
    scheduleHistoryForViewport(HISTORY_REQUEST_DEBOUNCE_MS);
  }
}

function snapToDevicePixel(
  value,
  dpr = Math.min(2, window.devicePixelRatio || 1)
) {
  return Math.round(Number(value || 0) * dpr) / dpr;
}

function alignedCanvasDelta(value) {
  // 缓存位图只按完整物理像素移动，杜绝 drawImage 在列边界做二次插值。
  return snapToDevicePixel(value);
}

function alignedVisualShift(value) {
  return alignedCanvasDelta(value);
}

function prepareCacheCanvas(targetCanvas, targetCtx, cssWidth, cssHeight, dpr) {
  const pixelWidth = Math.max(1, Math.round(cssWidth * dpr));
  const pixelHeight = Math.max(1, Math.round(cssHeight * dpr));
  // 尺寸不变时只清屏；反复赋 width/height 会重分配位图并放大实时顿挫。
  if (targetCanvas.width !== pixelWidth || targetCanvas.height !== pixelHeight) {
    targetCanvas.width = pixelWidth;
    targetCanvas.height = pixelHeight;
  }
  targetCtx.setTransform(1, 0, 0, 1, 0, 0);
  targetCtx.clearRect(0, 0, targetCanvas.width, targetCanvas.height);
  targetCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function currentCenterBucket(frameTime = performance.now()) {
  if (!viewport.follow && viewport.centerBucket !== null) {
    viewport.centerFrameAt = frameTime;
    return viewport.centerBucket;
  }
  if (state.mid !== null) {
    const targetBucket = priceToBucket(state.mid);
    if (targetBucket !== null) {
      const elapsed =
        viewport.centerFrameAt > 0
          ? Math.max(0, Math.min(250, frameTime - viewport.centerFrameAt))
          : visualFrameIntervalMs() || 1000 / 60;
      const alpha = 1 - Math.exp(-elapsed / CENTER_EASING_MS);
      viewport.centerBucket =
        viewport.centerBucket === null
          ? targetBucket
          : viewport.centerBucket + (targetBucket - viewport.centerBucket) * alpha;
      viewport.centerFrameAt = frameTime;
      return viewport.centerBucket;
    }
  }
  for (let i = state.columns.length - 1; i >= 0; i -= 1) {
    if (!state.columns[i].gap) {
      viewport.centerBucket = state.columns[i].center;
      viewport.centerFrameAt = frameTime;
      return viewport.centerBucket;
    }
  }
  return viewport.centerBucket;
}

function resetEdgeAutoRecenterState() {
  viewport.edgeResetLatched = false;
  viewport.edgeResetCooldownUntil = 0;
}

function maybeAutoRecenter(
  center,
  plotBottom,
  rowHeight,
  nowMs = Date.now()
) {
  if (
    state.appMode !== APP_MODE_LIVE ||
    viewport.follow ||
    viewport.dragging ||
    state.mid === null ||
    !Number.isFinite(center) ||
    !Number.isFinite(plotBottom) ||
    plotBottom <= 0 ||
    !Number.isFinite(rowHeight) ||
    rowHeight <= 0
  ) {
    return center;
  }
  const priceBucket = priceToBucket(Number(state.mid));
  if (priceBucket === null) return center;

  const centerY = plotBottom / 2;
  const priceY = centerY - (priceBucket - center) * rowHeight;
  const distanceFromEdgeRatio =
    Math.min(priceY, plotBottom - priceY) / plotBottom;
  if (viewport.edgeResetLatched) {
    if (distanceFromEdgeRatio >= EDGE_RESET_RELEASE_RATIO) {
      viewport.edgeResetLatched = false;
    } else {
      return center;
    }
  }
  if (
    distanceFromEdgeRatio <= EDGE_RESET_TRIGGER_RATIO &&
    nowMs >= viewport.edgeResetCooldownUntil
  ) {
    viewport.centerBucket = priceBucket;
    viewport.edgeResetLatched = true;
    viewport.edgeResetCooldownUntil = nowMs + EDGE_RESET_COOLDOWN_MS;
    viewport.edgeRecenterCount += 1;
    return priceBucket;
  }
  return center;
}

function render(frameTime = performance.now()) {
  heatmapCache.visualFrames += 1;
  if (frameStats.startedAt <= 0) frameStats.startedAt = frameTime;
  frameStats.frames += 1;
  const statsElapsed = frameTime - frameStats.startedAt;
  if (statsElapsed >= 1000) {
    frameStats.fps = (frameStats.frames * 1000) / statsElapsed;
    frameStats.startedAt = frameTime;
    frameStats.frames = 0;
  }
  const rect = canvas.getBoundingClientRect();
  const width = rect.width;
  const height = rect.height;
  ctx.fillStyle = "#04070b";
  ctx.fillRect(0, 0, width, height);

  const { axisWidth, domWidth } = chartSideWidths(width);
  const plotRight = Math.max(0, width - axisWidth - domWidth);
  const plotBottom = height - PLOT_BOTTOM;
  let center = currentCenterBucket(frameTime);
  if (center === null || !state.view) {
    ctx.fillStyle = "#4d6274";
    ctx.font = "13px ui-monospace, monospace";
    ctx.fillText("等待公共行情连接与首次订单簿同步…", 18, 28);
    return;
  }

  let geometry = priceViewportGeometry(plotBottom, center);
  let rowHeight = geometry.rowHeight;
  const recentered = maybeAutoRecenter(center, plotBottom, rowHeight);
  if (recentered !== center) {
    center = recentered;
    geometry = priceViewportGeometry(plotBottom, center);
    rowHeight = geometry.rowHeight;
  }
  // 中心永远由 mid 平滑跟随或用户平移决定；盘口最外档不再提供中点。
  // 薄盘口仅在首个有效 DOM 锁定一次 rowHeight，后续每帧都复用同一比例。
  center = geometry.center;
  viewport.rowHeight = rowHeight;
  const colWidth = viewport.colWidth;
  const centerY = plotBottom / 2;
  const bucketToY = (bucket) => centerY - (bucket - center) * rowHeight;
  const yToBucket = (y) => center - (y - centerY) / rowHeight;
  const topBucket = Math.ceil(yToBucket(0)) + 1;
  const bottomBucket = Math.floor(yToBucket(plotBottom)) - 1;
  const liveCoverage = geometry.coveragePct;
  viewport.renderedCoveragePct = liveCoverage;
  viewport.renderedBookCoveragePct = geometry.rawBookCoveragePct;
  viewport.bookOutsideViewport = geometry.needsBookRefit;
  viewport.priceScaleMode = geometry.fitMode;
  const timeShiftPx = alignedVisualShift(visualTimeShiftPx());
  const timeline = timelineGeometry(plotRight, colWidth, timeShiftPx);
  // 纵轴覆盖是当前画面状态，不只在缩放操作时提示；每次实时帧渲染都同步更新。
  updateCoverageDisplay(geometry, width);

  // 热图和盘口阶梯线只有新列/缩放/配色变化时重建；帧间只平移缓存位图。
  drawHeatmap(
    timeline.liveX,
    plotBottom,
    colWidth,
    rowHeight,
    bucketToY,
    topBucket,
    bottomBucket,
    center,
    timeShiftPx
  );
  // NOW 右侧只延伸当前真实订单簿；它不是带未来时间戳的热图列，更不是预测。
  drawLiveBookExtension(
    timeline.liveX,
    plotRight,
    plotBottom,
    rowHeight,
    bucketToY,
    topBucket,
    bottomBucket,
    center
  );
  // 顺序即层次：缓存热图与买卖阶梯线 → 气泡，气泡永远在历史热图最上层。
  drawBubbles(
    timeline.liveX,
    plotBottom,
    colWidth,
    bucketToY,
    timeShiftPx
  );
  drawHistoryQualityBand(
    timeline.liveX,
    plotBottom,
    colWidth,
    timeShiftPx
  );
  drawTimeAxis(
    plotRight,
    timeline.liveX,
    plotBottom,
    colWidth,
    height,
    timeShiftPx
  );
  drawDom(
    plotRight,
    plotBottom,
    rowHeight,
    bucketToY,
    topBucket,
    bottomBucket,
    domWidth
  );
  drawPriceAxis(
    width,
    plotRight,
    plotBottom,
    rowHeight,
    bucketToY,
    topBucket,
    bottomBucket,
    domWidth,
    axisWidth
  );
  drawPriceTags(width, plotRight, bucketToY, axisWidth);
  drawNowDivider(timeline.liveX, plotRight, plotBottom, height);

  const atBookEdge = geometry.atBookEdge;
  const nextScaleInfo =
    `黑位≤${fmtCompact(heatCutoffValue())} · 色阶→${fmtCompact(
      Math.max(1, state.scaleHi)
    )} · 对比 ${ui.heatContrast}% · 纵轴 ${liveCoverage.toFixed(2)}%` +
    (atBookEdge ? " · 已显示全部可信档位" : "");
  if (nextScaleInfo !== scaleInfoKey) {
    scaleInfoKey = nextScaleInfo;
    el("scaleInfo").textContent = nextScaleInfo;
  }
}

function paintHeatmap(
  targetCtx,
  plotRight,
  plotBottom,
  colWidth,
  rowHeight,
  bucketToY,
  topBucket,
  bottomBucket,
  startIndex = 0,
  firstLeftOverride = null
) {
  const columns = state.columns;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  let visitedColumns = 0;
  let renderedColumns = 0;
  const replayHistoryUnderlay =
    state.appMode === APP_MODE_REPLAY && state.historyColumns > 0;
  // 复盘合并阶段已让每个时间片只保留一个订单簿来源：可用的 1 秒历史
  // 优先，只有 archive hard gap / 尚未闭合的尾秒才保留冻结 live。
  // 因而这里不再做多分辨率 clearRect 拼接，避免再次挖出竖向暗缝。
  for (const historyLayer of [true, false]) {
    let lastPaintedRightDevicePx = null;
    for (let i = columns.length - 1; i >= startIndex; i -= 1) {
      const column = columns[i];
      if (Boolean(column.history) !== historyLayer) continue;
      visitedColumns += 1;
      const interval = columnPaintInterval(columns, i);
      if (!interval) continue;
      const rightRaw = historicalXForTime(
        interval.toMs,
        plotRight,
        colWidth,
        0
      );
      let leftRaw = historicalXForTime(
        interval.fromMs,
        plotRight,
        colWidth,
        0
      );
      if (
        i === startIndex &&
        Number.isFinite(firstLeftOverride) &&
        liveColumnsAreVisuallyContinuous(previousLiveColumn(columns, i), column)
      ) {
        // 增量缓存已经按累计物理像素位移；首个新列必须直接承接移动后的旧缓存
        // 右边界。若再按时间戳独立取整，累计余数偶尔会露出 1 个物理像素空缝。
        leftRaw = firstLeftOverride;
      }
      if (!Number.isFinite(leftRaw) || leftRaw >= rightRaw) {
        leftRaw = rightRaw - colWidth;
      }
      // 完整重建与新增列内部共用相邻时间戳边界；增量首列则已由上面的
      // firstLeftOverride 接住真实缓存边界。两条路径都只吸附一次物理像素。
      const left = snapToDevicePixel(leftRaw, dpr);
      const right = snapToDevicePixel(rightRaw, dpr);
      if (right < 0) break;
      if (left > plotRight) continue;
      if (column.gap) {
        // hard gap 只保留黑底和底部质量带，不能先占用亚像素代表位，
        // 否则同一物理像素内更老的有效列也会被错误跳过。
        continue;
      }
      const rightDevicePx = Math.round(right * dpr);
      // 横轴缩到一列不足一个物理像素时，每一层各自只保留该像素内最靠近
      // NOW 的快照；实时层随后覆盖历史层，既不聚合也不平均原始金额。
      if (rightDevicePx === lastPaintedRightDevicePx) {
        heatmapCache.temporalPixelSkips += 1;
        continue;
      }
      lastPaintedRightDevicePx = rightDevicePx;
      renderedColumns += 1;
      // 折叠后的时间区间放在其右边界左侧的一个物理像素内；旧写法从右
      // 边界向右画，会把最新亚像素列裁在缓存之外并留下尾部空白。
      const paintLeft = right <= left ? right - 1 / dpr : left;
      const width = Math.max(1 / dpr, right - paintLeft);
      if (!historyLayer && !replayHistoryUnderlay) {
        // 实时快照是该时间片的唯一高分辨率真值：先清掉历史底图，再画当前
        // 真实档位。实时模式没有历史底图，仍按列清理增量缓存尾部。
        targetCtx.clearRect(paintLeft, 0, width, plotBottom);
      }
      const bounds = heatScaleBounds(
        column.displayScaleLo,
        column.displayScaleHi
      );
      const rawCellSpan = Number(column.gridCellSpan);
      const cellSpan = Number.isFinite(rawCellSpan) && rawCellSpan > 0
        ? rawCellSpan
        : 1;
      const cellHeight = Math.max(1, rowHeight * cellSpan);
      drawColumnSide(
        targetCtx,
        column.bids,
        "bid",
        paintLeft,
        width,
        cellHeight,
        bucketToY,
        topBucket,
        bottomBucket,
        bounds,
        dpr
      );
      drawColumnSide(
        targetCtx,
        column.asks,
        "ask",
        paintLeft,
        width,
        cellHeight,
        bucketToY,
        topBucket,
        bottomBucket,
        bounds,
        dpr
      );
      // partial 秒保留真实热图；质量状态只在底部聚合带呈现。
    }
  }
  heatmapCache.lastPaintVisitedColumns = visitedColumns;
  heatmapCache.lastPaintRenderedColumns = renderedColumns;
}

function drawColumnSide(
  targetCtx,
  sideMap,
  side,
  x,
  colWidth,
  cellHeight,
  bucketToY,
  topBucket,
  bottomBucket,
  bounds,
  dpr
) {
  if (isTypedHistoryLevels(sideMap)) {
    for (let index = 0; index < sideMap.values.length; index += 1) {
      const bucket = sideMap.buckets[index];
      if (bucket > topBucket || bucket < bottomBucket) continue;
      const value = sideMap.values[index];
      if (value <= 0) continue;
      const color = heatColor(value, side, bounds);
      if (!color) continue;
      const bottom = snapToDevicePixel(bucketToY(bucket), dpr);
      const top = snapToDevicePixel(bucketToY(bucket) - cellHeight, dpr);
      targetCtx.fillStyle = color;
      targetCtx.fillRect(
        x,
        top,
        colWidth,
        Math.max(1 / dpr, bottom - top)
      );
    }
    return;
  }
  if (isFlatWireLevels(sideMap)) {
    for (let index = 0; index < sideMap.length; index += 2) {
      const bucket = Number(sideMap[index]);
      if (bucket > topBucket || bucket < bottomBucket) continue;
      const value = Number(sideMap[index + 1]);
      if (value <= 0) continue;
      const color = heatColor(value, side, bounds);
      if (!color) continue;
      const bottom = snapToDevicePixel(bucketToY(bucket), dpr);
      const top = snapToDevicePixel(bucketToY(bucket) - cellHeight, dpr);
      targetCtx.fillStyle = color;
      targetCtx.fillRect(
        x,
        top,
        colWidth,
        Math.max(1 / dpr, bottom - top)
      );
    }
    return;
  }
  for (const key in sideMap) {
    const bucket = Number(key);
    if (bucket > topBucket || bucket < bottomBucket) continue;
    const value = sideMap[key];
    if (value <= 0) continue;
    const color = heatColor(value, side, bounds);
    if (!color) continue;
    const bottom = snapToDevicePixel(bucketToY(bucket), dpr);
    const top = snapToDevicePixel(bucketToY(bucket) - cellHeight, dpr);
    targetCtx.fillStyle = color;
    targetCtx.fillRect(
      x,
      top,
      colWidth,
      Math.max(1 / dpr, bottom - top)
    );
  }
}

function rebuildHeatmapCache(
  key,
  plotRight,
  plotBottom,
  colWidth,
  rowHeight,
  topBucket,
  bottomBucket,
  center
) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  // timeline.liveX 已吸附到物理像素；缓存也使用同一个精确右边界，不保留
  // ceil 产生的透明尾像素，确保重建与后续增量位移的坐标原点一致。
  const cacheRight = Math.max(1 / dpr, snapToDevicePixel(plotRight, dpr));
  const cssWidth = cacheRight;
  const cssHeight = Math.max(1, Math.ceil(plotBottom + HEATMAP_CACHE_PAD_Y * 2));
  prepareCacheCanvas(heatmapCanvas, heatmapCtx, cssWidth, cssHeight, dpr);

  const extraBuckets = Math.ceil(HEATMAP_CACHE_PAD_Y / rowHeight) + 2;
  const cacheTopBucket = topBucket + extraBuckets;
  const cacheBottomBucket = bottomBucket - extraBuckets;
  const cacheCenterY = plotBottom / 2 + HEATMAP_CACHE_PAD_Y;
  const cacheBucketToY = (bucket) =>
    cacheCenterY - (bucket - center) * rowHeight;

  paintHeatmap(
    heatmapCtx,
    cacheRight,
    cssHeight,
    colWidth,
    rowHeight,
    cacheBucketToY,
    cacheTopBucket,
    cacheBottomBucket
  );
  drawBookLines(
    cacheRight,
    cssHeight,
    colWidth,
    cacheBucketToY,
    heatmapCtx,
    rowHeight
  );

  heatmapCache.key = key;
  heatmapCache.center = center;
  heatmapCache.cssWidth = cssWidth;
  heatmapCache.cssHeight = cssHeight;
  heatmapCache.revision = state.heatmapRevision;
  const newest = state.columns.length
    ? state.columns[state.columns.length - 1]
    : null;
  heatmapCache.latestSeq = newest ? newest.seq : null;
  heatmapCache.latestT = newest ? Number(newest.t) : null;
  heatmapCache.horizontalResidualPx = 0;
  heatmapCache.lastSeamCorrectionDevicePx = 0;
  heatmapCache.rebuilds += 1;
}

function shiftHeatmapBitmapLeft(shiftPx, dpr) {
  const shiftDevicePx = Math.round(shiftPx * dpr);
  if (
    shiftDevicePx <= 0 ||
    shiftDevicePx >= heatmapCanvas.width ||
    heatmapCanvas.width <= 1
  ) {
    return false;
  }
  if (
    heatmapShiftCanvas.width !== heatmapCanvas.width ||
    heatmapShiftCanvas.height !== heatmapCanvas.height
  ) {
    heatmapShiftCanvas.width = heatmapCanvas.width;
    heatmapShiftCanvas.height = heatmapCanvas.height;
  }
  const copyWidth = heatmapCanvas.width - shiftDevicePx;
  heatmapShiftCtx.setTransform(1, 0, 0, 1, 0, 0);
  heatmapShiftCtx.clearRect(
    0,
    0,
    heatmapShiftCanvas.width,
    heatmapShiftCanvas.height
  );
  heatmapShiftCtx.imageSmoothingEnabled = false;
  heatmapShiftCtx.drawImage(
    heatmapCanvas,
    shiftDevicePx,
    0,
    copyWidth,
    heatmapCanvas.height,
    0,
    0,
    copyWidth,
    heatmapCanvas.height
  );
  heatmapCtx.setTransform(1, 0, 0, 1, 0, 0);
  heatmapCtx.clearRect(0, 0, heatmapCanvas.width, heatmapCanvas.height);
  heatmapCtx.imageSmoothingEnabled = false;
  heatmapCtx.drawImage(heatmapShiftCanvas, 0, 0);
  heatmapCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return true;
}

function appendHeatmapCacheColumns(
  plotRight,
  plotBottom,
  colWidth,
  rowHeight,
  dpr
) {
  const columns = state.columns;
  if (
    !columns.length ||
    heatmapCache.latestSeq === null ||
    !Number.isFinite(heatmapCache.latestT) ||
    heatmapCache.center === null
  ) {
    return false;
  }
  const previousIndex = columns.findIndex(
    (column) => column.seq === heatmapCache.latestSeq
  );
  if (previousIndex < 0 || previousIndex >= columns.length - 1) return false;
  const newest = columns[columns.length - 1];
  const newestT = Number(newest.t);
  if (!Number.isFinite(newestT) || newestT <= heatmapCache.latestT) return false;

  const exactShiftPx =
    ((newestT - heatmapCache.latestT) / columnIntervalMs()) * colWidth;
  const shiftWithResidual =
    exactShiftPx + heatmapCache.horizontalResidualPx;
  const shiftPx = snapToDevicePixel(shiftWithResidual, dpr);
  const cacheRight = snapToDevicePixel(heatmapCache.cssWidth, dpr);
  if (
    cacheRight <= 1 / dpr ||
    Math.abs(cacheRight - snapToDevicePixel(plotRight, dpr)) > 0.01 ||
    shiftPx < 0 ||
    shiftPx >= cacheRight - 1 / dpr
  ) {
    return false;
  }

  const firstNewIndex = previousIndex + 1;
  const previousNaturalInterval = columnTimeInterval(columns, previousIndex);
  const previousPaintInterval = columnPaintInterval(columns, previousIndex);
  const repaintPreviousHold = Boolean(
    liveContinuousHeatmapEnabled() &&
    previousNaturalInterval &&
    previousPaintInterval &&
    previousPaintInterval.toMs > previousNaturalInterval.toMs
  );
  const paintStartIndex = repaintPreviousHold ? previousIndex : firstNewIndex;
  const shiftedPreviousRight = cacheRight - shiftPx;
  const firstNewRight = snapToDevicePixel(
    historicalXForTime(
      columns[firstNewIndex].t,
      cacheRight,
      colWidth,
      0
    ),
    dpr
  );
  if (
    !Number.isFinite(firstNewRight) ||
    (shiftPx > 0 && firstNewRight <= shiftedPreviousRight)
  ) {
    return false;
  }

  // 仅用于验收与现场诊断：记录旧算法会产生的独立取整偏差。正式绘制始终
  // 使用 shiftedPreviousRight，因此计数增加不代表画面仍有缝隙。
  const timestampBoundary = snapToDevicePixel(
    historicalXForTime(
      columns[previousIndex].t,
      cacheRight,
      colWidth,
      0
    ),
    dpr
  );
  const seamCorrectionDevicePx = Math.round(
    (timestampBoundary - shiftedPreviousRight) * dpr
  );
  if (shiftPx > 0 && !shiftHeatmapBitmapLeft(shiftPx, dpr)) return false;
  heatmapCache.horizontalResidualPx = shiftWithResidual - shiftPx;
  heatmapCache.lastSeamCorrectionDevicePx = seamCorrectionDevicePx;
  if (seamCorrectionDevicePx !== 0) {
    heatmapCache.seamCorrections += 1;
    heatmapCache.maxSeamCorrectionDevicePx = Math.max(
      heatmapCache.maxSeamCorrectionDevicePx,
      Math.abs(seamCorrectionDevicePx)
    );
  }

  const extraBuckets = Math.ceil(HEATMAP_CACHE_PAD_Y / rowHeight) + 2;
  const cacheTopBucket =
    Math.ceil(heatmapCache.center + plotBottom / 2 / rowHeight) +
    1 +
    extraBuckets;
  const cacheBottomBucket =
    Math.floor(heatmapCache.center - plotBottom / 2 / rowHeight) -
    1 -
    extraBuckets;
  const cacheCenterY = plotBottom / 2 + HEATMAP_CACHE_PAD_Y;
  const cacheBucketToY = (bucket) =>
    cacheCenterY - (bucket - heatmapCache.center) * rowHeight;
  const tailLeft = Math.max(
    0,
    shiftPx > 0 ? shiftedPreviousRight - 1 / dpr : cacheRight - 1 / dpr
  );

  heatmapCtx.save();
  heatmapCtx.beginPath();
  heatmapCtx.rect(
    tailLeft,
    0,
    Math.max(1 / dpr, cacheRight - tailLeft),
    heatmapCache.cssHeight
  );
  heatmapCtx.clip();
  if (shiftPx > 0) {
    paintHeatmap(
      heatmapCtx,
      cacheRight,
      heatmapCache.cssHeight,
      colWidth,
      rowHeight,
      cacheBucketToY,
      cacheTopBucket,
      cacheBottomBucket,
      paintStartIndex,
      repaintPreviousHold ? null : shiftedPreviousRight
    );
  } else {
    // 本次真实位移尚不足一个物理像素：保留累计余数，只清理并更新 NOW
    // 左侧最后一个像素。绝不能因此退回整张历史重建。
    heatmapCtx.clearRect(
      tailLeft,
      0,
      1 / dpr,
      heatmapCache.cssHeight
    );
    paintHeatmap(
      heatmapCtx,
      cacheRight,
      heatmapCache.cssHeight,
      colWidth,
      rowHeight,
      cacheBucketToY,
      cacheTopBucket,
      cacheBottomBucket,
      paintStartIndex,
      repaintPreviousHold ? null : tailLeft
    );
    heatmapCache.subpixelTailUpdates += 1;
  }
  // 阶梯线远比逐价格档热图便宜；在尾部裁剪内重画可保持跨列转折连续。
  drawBookLines(
    cacheRight,
    heatmapCache.cssHeight,
    colWidth,
    cacheBucketToY,
    heatmapCtx,
    rowHeight,
    Math.max(0, previousIndex - 1)
  );
  heatmapCtx.restore();

  heatmapCache.revision = state.heatmapRevision;
  heatmapCache.latestSeq = newest.seq;
  heatmapCache.latestT = newestT;
  heatmapCache.incrementalUpdates += 1;
  return true;
}

function drawHeatmap(
  plotRight,
  plotBottom,
  colWidth,
  rowHeight,
  bucketToY,
  topBucket,
  bottomBucket,
  center,
  timeShiftPx
) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const key = [
    state.streamEpoch,
    state.view && state.view.gridEpoch,
    state.view && state.view.gridAnchor,
    state.view && state.view.gridRatio,
    ui.colorMode,
    ui.heatContrast,
    ui.continuousHeatmap,
    Math.round(plotRight * 10),
    Math.round(plotBottom * 10),
    // 10 分钟端 colWidth 只有百分之几像素；两位小数会让相邻滚轮档
    // 复用旧时间尺度缓存，气泡/时间轴已缩放而热图短暂错位。
    Math.round(colWidth * 1_000_000),
    Math.round(rowHeight * 1000),
    Math.round(dpr * 10),
  ].join(":");
  let yShift = alignedCanvasDelta(
    heatmapCache.center === null
      ? 0
      : (center - heatmapCache.center) * rowHeight
  );
  if (
    heatmapCache.key !== key ||
    heatmapCache.center === null ||
    Math.abs(yShift) >= HEATMAP_CACHE_REBUILD_SHIFT_PX
  ) {
    rebuildHeatmapCache(
      key,
      plotRight,
      plotBottom,
      colWidth,
      rowHeight,
      topBucket,
      bottomBucket,
      center
    );
    yShift = 0;
  } else if (
    heatmapCache.revision !== state.heatmapRevision &&
    !appendHeatmapCacheColumns(
      plotRight,
      plotBottom,
      colWidth,
      rowHeight,
      dpr
    )
  ) {
    rebuildHeatmapCache(
      key,
      plotRight,
      plotBottom,
      colWidth,
      rowHeight,
      topBucket,
      bottomBucket,
      center
    );
    yShift = 0;
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, plotRight, plotBottom);
  ctx.clip();
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(
    heatmapCanvas,
    -timeShiftPx,
    -HEATMAP_CACHE_PAD_Y + yShift,
    heatmapCanvas.width / dpr,
    heatmapCanvas.height / dpr
  );
  // 缓存向左平移后，最后一份已确认历史快照按 sample-and-hold 延伸到
  // 固定 NOW 线。这样历史/当前的真实视觉边界永远就是 NOW，不再借用
  // 当前盘口向左补洞，也不会每个采样周期出现明暗交界扫动。
  const heldTailWidth = Math.min(plotRight, Math.max(0, timeShiftPx));
  if (heldTailWidth > 0) {
    const sourceRight = Math.min(
      heatmapCanvas.width,
      Math.max(1, Math.round(plotRight * dpr))
    );
    const sourceWidth = 1;
    ctx.drawImage(
      heatmapCanvas,
      Math.max(0, sourceRight - sourceWidth),
      0,
      sourceWidth,
      heatmapCanvas.height,
      plotRight - heldTailWidth,
      -HEATMAP_CACHE_PAD_Y + yShift,
      heldTailWidth + 1 / dpr,
      heatmapCanvas.height / dpr
    );
  }
  ctx.restore();
}

function drawHistoryQualityBand(liveX, plotBottom, colWidth, timeShiftPx) {
  if (state.appMode !== APP_MODE_REPLAY || liveX <= 0) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const pixelWidth = Math.max(1, Math.ceil(liveX * dpr));
  const quality = new Uint8Array(pixelWidth);
  const liveCoverageRanges = mergedLiveCoverageIntervals(state.columns);
  let hasQuality = false;
  for (let index = 0; index < state.columns.length; index += 1) {
    const column = state.columns[index];
    const level = column && column.history
      ? column.gap
        ? 2
        : column.partialCoverage
          ? 1
          : 0
      : 0;
    if (!level) continue;
    const interval = columnTimeInterval(state.columns, index);
    if (!interval) continue;
    if (
      timeIntervalFullyCoveredByRanges(
        liveCoverageRanges,
        interval.fromMs,
        interval.toMs
      )
    ) {
      // 黄带表示最终复合画面仍存在未覆盖区；若该历史秒已被
      // 有效 live 完整接管，不再把原始 archive partial/gap 误报给用户。
      continue;
    }
    const left = historicalXForTime(
      interval.fromMs,
      liveX,
      colWidth,
      timeShiftPx
    );
    const right = historicalXForTime(
      interval.toMs,
      liveX,
      colWidth,
      timeShiftPx
    );
    if (right <= 0 || left >= liveX) continue;
    const fromPixel = Math.max(0, Math.floor(Math.min(left, right) * dpr));
    const toPixel = Math.min(
      pixelWidth,
      Math.max(fromPixel + 1, Math.ceil(Math.max(left, right) * dpr))
    );
    for (let pixel = fromPixel; pixel < toPixel; pixel += 1) {
      if (quality[pixel] < level) quality[pixel] = level;
    }
    hasQuality = true;
  }
  if (!hasQuality) return;

  const bandHeight = 4;
  let runStart = 0;
  let runLevel = quality[0];
  const paintRun = (endPixel) => {
    if (!runLevel || endPixel <= runStart) return;
    ctx.fillStyle = runLevel === 2
      ? "rgba(245,158,11,0.82)"
      : "rgba(245,158,11,0.38)";
    ctx.fillRect(
      runStart / dpr,
      plotBottom - bandHeight,
      Math.max(1 / dpr, (endPixel - runStart) / dpr),
      bandHeight
    );
  };
  for (let pixel = 1; pixel <= pixelWidth; pixel += 1) {
    const nextLevel = pixel < pixelWidth ? quality[pixel] : -1;
    if (nextLevel === runLevel) continue;
    paintRun(pixel);
    runStart = pixel;
    runLevel = nextLevel;
  }
}

function liveBookIsCurrent() {
  return (
    transportConnected &&
    liveBookFresh &&
    !!state.dom &&
    !!state.dom.ready &&
    !!state.status &&
    state.status.overall === "LIVE"
  );
}

function rebuildLiveBookCache(
  key,
  gutterWidth,
  plotBottom,
  rowHeight,
  topBucket,
  bottomBucket,
  center
) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cssWidth = Math.max(1, Math.ceil(gutterWidth));
  const cssHeight = Math.max(1, Math.ceil(plotBottom + HEATMAP_CACHE_PAD_Y * 2));
  prepareCacheCanvas(liveBookCanvas, liveBookCtx, cssWidth, cssHeight, dpr);

  if (liveBookIsCurrent()) {
    const extraBuckets = Math.ceil(HEATMAP_CACHE_PAD_Y / rowHeight) + 2;
    const cacheTopBucket = topBucket + extraBuckets;
    const cacheBottomBucket = bottomBucket - extraBuckets;
    const cacheCenterY = plotBottom / 2 + HEATMAP_CACHE_PAD_Y;
    const cacheBucketToY = (bucket) =>
      cacheCenterY - (bucket - center) * rowHeight;
    const cellHeight = Math.max(1, rowHeight);
    const drawRows = (side) => {
      forEachDomRow(state.dom, `${side}s`, (bucket, value, trusted) => {
        if (
          !trusted ||
          !Number.isFinite(bucket) ||
          !Number.isFinite(value) ||
          value <= 0 ||
          bucket > cacheTopBucket ||
          bucket < cacheBottomBucket
        ) {
          return;
        }
        const color = heatColor(value, side);
        if (!color) return;
        const bottom = snapToDevicePixel(cacheBucketToY(bucket), dpr);
        const top = snapToDevicePixel(
          cacheBucketToY(bucket) - cellHeight,
          dpr
        );
        liveBookCtx.fillStyle = color;
        liveBookCtx.fillRect(
          0,
          top,
          cssWidth,
          Math.max(1 / dpr, bottom - top)
        );
      });
    };
    liveBookCtx.save();
    liveBookCtx.globalAlpha = LIVE_BOOK_OPACITY;
    drawRows("bid");
    drawRows("ask");
    liveBookCtx.restore();
  }

  liveBookCache.key = key;
  liveBookCache.center = center;
  liveBookCache.cssWidth = cssWidth;
  liveBookCache.cssHeight = cssHeight;
  liveBookCache.rebuilds += 1;
}

function drawLiveBookExtension(
  liveX,
  plotRight,
  plotBottom,
  rowHeight,
  bucketToY,
  topBucket,
  bottomBucket,
  center
) {
  if (state.appMode === APP_MODE_REPLAY) return;
  const gutterWidth = Math.max(1, plotRight - liveX);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const isCurrent = liveBookIsCurrent();
  // 当前盘口严格从固定 NOW 开始。NOW 左侧由最后一份历史快照延伸补齐，
  // 不再让当前盘口桥接区随帧率/采样周期左右扫动。
  const liveWidth = Math.max(1, plotRight - liveX);
  ctx.fillStyle = "#071019";
  ctx.fillRect(liveX, 0, gutterWidth, plotBottom);

  const key = [
    state.streamEpoch,
    state.view && state.view.gridEpoch,
    state.view && state.view.gridAnchor,
    state.view && state.view.gridRatio,
    state.domRevision,
    ui.colorMode,
    ui.heatContrast,
    state.status && state.status.overall,
    Math.round(gutterWidth * 10),
    Math.round(plotBottom * 10),
    Math.round(rowHeight * 1000),
    Math.round(dpr * 10),
  ].join(":");
  let yShift = alignedCanvasDelta(
    liveBookCache.center === null
      ? 0
      : (center - liveBookCache.center) * rowHeight
  );
  if (
    liveBookCache.key !== key ||
    liveBookCache.center === null ||
    Math.abs(yShift) >= HEATMAP_CACHE_REBUILD_SHIFT_PX
  ) {
    rebuildLiveBookCache(
      key,
      gutterWidth,
      plotBottom,
      rowHeight,
      topBucket,
      bottomBucket,
      center
    );
    yShift = 0;
  }

  if (isCurrent) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(liveX, 0, liveWidth, plotBottom);
    ctx.clip();
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(
      liveBookCanvas,
      0,
      0,
      liveBookCanvas.width,
      liveBookCanvas.height,
      liveX,
      -HEATMAP_CACHE_PAD_Y + yShift,
      liveWidth,
      liveBookCanvas.height / dpr
    );
    const midBucket = priceToBucket(Number(state.mid));
    if (midBucket !== null) {
      const midY = bucketToY(midBucket) - rowHeight / 2;
      if (midY >= 0 && midY <= plotBottom) {
        ctx.setLineDash([6, 5]);
        ctx.strokeStyle = "rgba(226,232,240,0.68)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(liveX, midY + 0.5);
        ctx.lineTo(plotRight, midY + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }
    ctx.restore();
  }
}

function drawNowDivider(liveX, plotRight, plotBottom, height) {
  const replay = state.appMode === APP_MODE_REPLAY;
  const isCurrent = liveBookIsCurrent();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  ctx.save();
  ctx.strokeStyle = replay
    ? "rgba(96,165,250,0.86)"
    : isCurrent
      ? "rgba(226,232,240,0.86)"
      : "rgba(245,158,11,0.8)";
  ctx.lineWidth = 1 / dpr;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  // liveX 已吸附到物理像素边界；虚线中心放在其右侧半个物理像素，
  // 既保持清晰，也让线的左边缘精确等于历史/当前分界。
  const dividerX = liveX + 0.5 / dpr;
  ctx.moveTo(dividerX, 0);
  ctx.lineTo(dividerX, plotBottom);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.font = "bold 9px ui-monospace, monospace";
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  ctx.fillStyle = "#dbe7f1";
  ctx.fillText(
    replay ? "复盘终点" : "NOW",
    liveX - 6,
    plotBottom + (height - plotBottom) / 2
  );

  if (replay) {
    ctx.fillStyle = "#93c5fd";
    ctx.fillText(
      "已冻结",
      liveX - 70,
      plotBottom + (height - plotBottom) / 2
    );
    ctx.restore();
    return;
  }

  // 当前盘口延伸区的产品文案固定强调“当前挂单”，避免被误读成未来预测。
  const label = isCurrent
    ? "当前挂单 · 非预测"
    : transportConnected
      ? "挂单同步中 · 非预测"
      : "行情已断开 · 非预测";
  ctx.textAlign = "center";
  ctx.fillStyle = isCurrent ? "#8aa3b7" : "#d4a653";
  ctx.fillText(
    label,
    liveX + (plotRight - liveX) / 2,
    plotBottom + (height - plotBottom) / 2
  );
  ctx.restore();
}

/*
 * 成交气泡采用 ATAS 向的柔和球面，但不伪装成真正 3D：
 *
 * 1. 球体本身由小面积柔光平滑过渡到深色边缘，不画独立外圈；
 * 2. 只用模糊投影把气泡从热图上托出，不使用硬描边或完整高光环；
 * 3. 大单只增强饱和度、柔光和尺寸，不再添加容易误读为新数据的“内核”；
 * 4. 半径读取气泡到达时冻结的品种画像结果；绘制阶段不读取当前 q/p90，旧气泡不呼吸，
 *    小的先画、大的后画；
 * 5. 预渲染半像素半径档位的 sprite，避免每帧为大量气泡重复创建渐变。
 */
function bubbleSprite(radius, buy, emphasis) {
  const radiusBucket = Math.round(radius * 2) / 2;
  const key = `${buy ? "buy" : "sell"}:${emphasis ? "emphasis" : "normal"}:${radiusBucket}`;
  const cached = bubbleSpriteCache.get(key);
  if (cached) return cached;

  const dpr = 2;
  const padding = BUBBLE_SPRITE_PADDING_PX;
  const cssSize = Math.ceil((radiusBucket + padding) * 2);
  const sprite = document.createElement("canvas");
  sprite.width = cssSize * dpr;
  sprite.height = cssSize * dpr;
  const spriteCtx = sprite.getContext("2d");
  spriteCtx.scale(dpr, dpr);

  const center = cssSize / 2;
  const highlightX = center + radiusBucket * 0.14;
  const highlightY = center - radiusBucket * 0.16;

  const body = spriteCtx.createRadialGradient(
    highlightX,
    highlightY,
    Math.max(0.3, radiusBucket * 0.025),
    center,
    center,
    radiusBucket * 1.02
  );
  if (buy) {
    body.addColorStop(0, emphasis ? "rgba(136,255,221,0.98)" : "rgba(116,242,207,0.94)");
    body.addColorStop(0.12, emphasis ? "rgba(39,218,174,0.97)" : "rgba(32,200,159,0.91)");
    body.addColorStop(0.46, emphasis ? "rgba(12,163,128,0.94)" : "rgba(12,146,116,0.88)");
    body.addColorStop(0.78, emphasis ? "rgba(5,112,93,0.95)" : "rgba(5,96,81,0.9)");
    body.addColorStop(1, emphasis ? "rgba(3,60,56,0.98)" : "rgba(3,53,50,0.94)");
  } else {
    body.addColorStop(0, emphasis ? "rgba(255,177,202,0.99)" : "rgba(255,158,188,0.95)");
    body.addColorStop(0.12, emphasis ? "rgba(255,73,126,0.98)" : "rgba(244,62,114,0.92)");
    body.addColorStop(0.46, emphasis ? "rgba(215,31,88,0.95)" : "rgba(197,29,80,0.89)");
    body.addColorStop(0.78, emphasis ? "rgba(151,18,66,0.96)" : "rgba(132,17,59,0.91)");
    body.addColorStop(1, emphasis ? "rgba(74,10,44,0.98)" : "rgba(65,9,39,0.95)");
  }

  // ATAS 参考里边缘是球体自身的暗部；这里只加模糊投影，不再多画一层圆。
  spriteCtx.shadowColor = buy
    ? emphasis
      ? "rgba(6,153,123,0.5)"
      : "rgba(1,9,11,0.52)"
    : emphasis
      ? "rgba(209,28,82,0.5)"
      : "rgba(1,9,11,0.52)";
  spriteCtx.shadowBlur = emphasis ? 4.2 : 3.2;
  spriteCtx.shadowOffsetY = 0.8;
  spriteCtx.beginPath();
  spriteCtx.arc(center, center, radiusBucket, 0, TAU);
  spriteCtx.fillStyle = body;
  spriteCtx.fill();
  spriteCtx.shadowColor = "transparent";
  spriteCtx.shadowBlur = 0;
  spriteCtx.shadowOffsetY = 0;

  const result = { canvas: sprite, size: cssSize };
  bubbleSpriteCache.set(key, result);
  return result;
}

function bubbleYForGridPosition(position, bucketToY) {
  // bubble.bucket 现在是单笔成交真实价格在当前网格中的连续位置，不再是
  // 一个待居中的整数 cell。额外减半行会把所有成交固定上移 0.5 格。
  return bucketToY(position);
}

function drawBubbles(liveX, plotBottom, colWidth, bucketToY, timeShiftPx) {
  const columns = state.columns;
  bubbleRenderStats.lastVisited = 0;
  bubbleRenderStats.lastRendered = 0;
  bubbleRenderStats.lastVisibleFromMs = null;
  if (!columns.length || !state.bubbles.length) return;
  const firstT = Number(columns[0].t);
  const newestT = timelineAnchorTimeMs();
  const pxPerMs = colWidth / columnIntervalMs();
  const visibleDurationMs =
    pxPerMs > 0
      ? Math.max(0, liveX + BUBBLE_RENDER_MARGIN_PX - timeShiftPx) / pxPerMs
      : 0;
  const visibleFromMs = Math.max(firstT, newestT - visibleDurationMs);
  const firstVisibleIndex = lowerBoundBubbleEventTime(
    state.bubbles,
    visibleFromMs
  );
  bubbleRenderStats.lastVisibleFromMs = visibleFromMs;
  const scale = ui.bubbleScale;

  // 三档顺序代替逐帧排序：小气泡先画，巨单最后覆盖；数据越多收益越明显。
  for (const layer of bubbleDrawLayers) layer.length = 0;
  let pooledItems = 0;
  for (let i = state.bubbles.length - 1; i >= firstVisibleIndex; i -= 1) {
    bubbleRenderStats.lastVisited += 1;
    const bubble = state.bubbles[i];
    const strength = Number(bubble.displayStrength);
    if (!Number.isFinite(strength)) continue;
    const notional = Number(bubble.notional);
    if (!Number.isFinite(notional) || notional < activeTradeMinNotional()) continue;
    // 每个气泡使用该条上游成交事件自身的成交时间；中心永远不越过 NOW，
    // 右侧当前挂单区不允许出现已成交气泡。
    const x = Math.min(
      liveX,
      historicalXForTime(
        bubbleEventTimeMs(bubble),
        liveX,
        colWidth,
        timeShiftPx
      )
    );
    if (x < -BUBBLE_RENDER_MARGIN_PX || x > liveX) continue;
    const y = bubbleYForGridPosition(bubble.bucket, bucketToY);
    if (
      y < -BUBBLE_RENDER_MARGIN_PX ||
      y > plotBottom + BUBBLE_RENDER_MARGIN_PX
    ) {
      continue;
    }
    let item = bubbleDrawItemPool[pooledItems];
    if (!item) {
      item = {};
      bubbleDrawItemPool.push(item);
    }
    pooledItems += 1;
    item.x = x;
    item.y = y;
    item.radius = bubbleRadiusPx(bubble.displayRadiusUnit, scale);
    item.buy = bubble.side === "buy";
    item.emphasis = !!bubble.displayEmphasis;
    const layer = strength >= 3 ? 2 : strength >= 1 ? 1 : 0;
    bubbleDrawLayers[layer].push(item);
    bubbleRenderStats.lastRendered += 1;
  }

  for (const layer of bubbleDrawLayers) {
    for (const item of layer) {
      const sprite = bubbleSprite(item.radius, item.buy, item.emphasis);
      ctx.drawImage(
        sprite.canvas,
        item.x - sprite.size / 2,
        item.y - sprite.size / 2,
        sprite.size,
        sprite.size
      );
    }
  }
}

/*
 * 买卖阶梯线：用每列已有的 bestBid / bestAsk bucket 画出盘口随时间的走势。
 * 这是 Quantower 三种配色下都保留的骨架线——热图再花，价格轨迹也清楚。
 */
function drawBookLines(
  plotRight,
  plotBottom,
  colWidth,
  bucketToY,
  targetCtx = ctx,
  rowHeight = viewport.rowHeight,
  startIndex = 0
) {
  const columns = state.columns;
  if (columns.length < 2) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const liveColumns = columns.filter((column) => !column.history);

  const tracePath = (field, historyLayer) => {
    targetCtx.beginPath();
    let started = false;
    let previousLayerColumn = null;
    let previousLayerInterval = null;
    for (let i = Math.max(0, startIndex); i < columns.length; i += 1) {
      const column = columns[i];
      if (Boolean(column.history) !== historyLayer) continue;
      const interval = columnPaintInterval(columns, i);
      if (!interval) continue;
      if (
        historyLayer &&
        historyIntervalHasAnyLiveCoverage(
          liveColumns,
          interval.fromMs,
          interval.toMs
        )
      ) {
        // 重叠时间只画实时 best bid/ask，避免历史均值骨架与实时骨架双线；
        // 历史热图底层仍保留，用于未被实时列覆盖的时间片。
        started = false;
        previousLayerColumn = column;
        previousLayerInterval = interval;
        continue;
      }
      const bucket = Number(column[field]);
      const timeContinuous = previousLayerColumn !== null && (
        historyLayer
          ? !previousLayerColumn.gap &&
            !column.gap &&
            previousLayerInterval !== null &&
            interval.fromMs <= previousLayerInterval.toMs
          : liveColumnsAreVisuallyContinuous(previousLayerColumn, column)
      );
      const priceContinuous =
        timeContinuous &&
        bookLineBucketsAreVisuallyContinuous(
          previousLayerColumn[field],
          bucket,
          rowHeight
        );
      const layerContinuous = timeContinuous && priceContinuous;
      if (started && !layerContinuous) started = false;
      previousLayerColumn = column;
      previousLayerInterval = interval;
      const x1 = historicalXForTime(interval.toMs, plotRight, colWidth, 0);
      let nextIndex = i + 1;
      while (
        nextIndex < columns.length &&
        Boolean(columns[nextIndex].history) !== historyLayer
      ) {
        nextIndex += 1;
      }
      if (nextIndex < columns.length) {
        const nextInterval = columnPaintInterval(columns, nextIndex);
        const nextX = historicalXForTime(
          nextInterval ? nextInterval.toMs : columns[nextIndex].t,
          plotRight,
          colWidth,
          0
        );
        // 与热图相同，阶梯线在同一物理时间像素内只保留最新快照。
        if (Math.round(nextX * dpr) === Math.round(x1 * dpr)) {
          heatmapCache.bookLinePixelSkips += 1;
          continue;
        }
      }
      let x0 = historicalXForTime(
        interval.fromMs,
        plotRight,
        colWidth,
        0
      );
      if (!Number.isFinite(x0) || x0 >= x1) x0 = x1 - colWidth;
      if (Math.round(x0 * dpr) === Math.round(x1 * dpr)) {
        x0 = x1 - 1 / dpr;
      }
      if (x1 < 0) continue;
      if (x0 > plotRight) continue;
      if (column.gap || !Number.isFinite(bucket)) {
        started = false;
        continue;
      }
      const rawCellSpan = Number(column.gridCellSpan);
      const cellSpan = Number.isFinite(rawCellSpan) && rawCellSpan > 0
        ? rawCellSpan
        : 1;
      const y = bucketToY(bucket) - rowHeight * cellSpan / 2;
      if (!started) {
        targetCtx.moveTo(x0, y);
        started = true;
      } else {
        targetCtx.lineTo(x0, y);
      }
      targetCtx.lineTo(x1, y);
    }
  };

  targetCtx.lineJoin = "round";
  targetCtx.lineCap = "butt";
  for (const [field, color] of [
    ["bestAsk", "rgba(248,90,80,0.95)"],
    ["bestBid", "rgba(45,212,160,0.95)"],
  ]) {
    for (const historyLayer of [true, false]) {
      // 与热图相同：历史先兜底，实时随后覆盖；每层先描暗底，亮区仍清楚。
      tracePath(field, historyLayer);
      targetCtx.strokeStyle = "rgba(3,6,10,0.75)";
      targetCtx.lineWidth = 3.8;
      targetCtx.stroke();
      tracePath(field, historyLayer);
      targetCtx.strokeStyle = color;
      targetCtx.lineWidth = 1.8;
      targetCtx.stroke();
    }
  }
}

function drawTimeAxis(
  plotRight,
  liveX,
  plotBottom,
  colWidth,
  height,
  timeShiftPx
) {
  const columns = state.columns;
  ctx.fillStyle = "#0a1017";
  ctx.fillRect(0, plotBottom, plotRight, height - plotBottom);
  ctx.fillStyle = "#5d7183";
  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "middle";
  if (!columns.length) return;
  const newestT = timelineAnchorTimeMs();
  const pxPerMs = colWidth / columnIntervalMs();
  if (!Number.isFinite(newestT) || pxPerMs <= 0) return;
  const visualNowT = newestT + timeShiftPx / pxPerMs;
  const minStepMs = 70 / pxPerMs;
  const stepMs =
    TIME_AXIS_STEPS_MS.find((candidate) => candidate >= minStepMs) ||
    TIME_AXIS_STEPS_MS[TIME_AXIS_STEPS_MS.length - 1];
  // 固定到真实整秒/整分钟，而不是从“最新列下标”倒数。新列到达后刻度锚点
  // 不再整体换一组，因此刻度与热图共用同一个连续视觉时钟、同速左移。
  let tickT = Math.floor(visualNowT / stepMs) * stepMs;
  const maxTicks = Math.ceil(liveX / (stepMs * pxPerMs)) + 2;
  for (let tick = 0; tick < maxTicks; tick += 1, tickT -= stepMs) {
    const x = snapToDevicePixel(
      liveX - (visualNowT - tickT) * pxPerMs
    );
    if (x < 30) break;
    if (x > liveX) continue;
    // 给 NOW 标签保留固定槽位，避免最新秒刻度与分界文案挤在一起。
    if (x > liveX - 58) continue;
    ctx.fillText(fmtClock(tickT), x - 22, plotBottom + 10);
    ctx.fillStyle = "rgba(93,113,131,0.10)";
    ctx.fillRect(x, 0, 1, plotBottom);
    ctx.fillStyle = "#5d7183";
  }
}

function drawDom(
  plotRight,
  plotBottom,
  rowHeight,
  bucketToY,
  topBucket,
  bottomBucket,
  domWidth
) {
  const dom = state.dom;
  const left = plotRight;
  const headerH = 38;
  ctx.fillStyle = "#070d14";
  ctx.fillRect(left, 0, domWidth, plotBottom);
  ctx.strokeStyle = "#1b2836";
  ctx.beginPath();
  ctx.moveTo(left + 0.5, 0);
  ctx.lineTo(left + 0.5, plotBottom);
  ctx.stroke();
  if (!dom || !dom.ready) return;

  const metrics = currentDomMetrics();
  const maxValue = metrics.maxValue;

  const cellHeight = Math.max(1, rowHeight - 0.5);
  const barMax = domWidth - 8;
  const rightEdge = left + domWidth - 3;
  const drawRow = (bucket, notionalValue, trusted, color) => {
    const y = bucketToY(bucket) - cellHeight;
    if (y < headerH || y > plotBottom) return;
    if (bucket > topBucket || bucket < bottomBucket) return;
    const width = Math.min(barMax, (notionalValue / maxValue) * barMax);
    if (notionalValue <= 0) return;
    // 从右到左：贴着价轴一侧向左生长。
    if (!trusted) {
      ctx.strokeStyle = color.replace("0.9", "0.35");
      ctx.lineWidth = 1;
      ctx.strokeRect(
        rightEdge - Math.max(1, width) + 0.5,
        y + 0.5,
        Math.max(1, width),
        Math.max(1, cellHeight - 1)
      );
    } else {
      ctx.fillStyle = color;
      ctx.fillRect(rightEdge - Math.max(1, width), y, Math.max(1, width), cellHeight);
    }
  };
  forEachDomRow(dom, "bids", (bucket, value, trusted) => {
    drawRow(bucket, value, trusted, "rgba(22,163,74,0.9)");
  });
  forEachDomRow(dom, "asks", (bucket, value, trusted) => {
    drawRow(bucket, value, trusted, "rgba(220,38,38,0.9)");
  });

  const totalBid = metrics.totalBid;
  const totalAsk = metrics.totalAsk;
  const sum = totalBid + totalAsk;
  const bidPct = sum > 0 ? (totalBid / sum) * 100 : 0;
  const askPct = sum > 0 ? (totalAsk / sum) * 100 : 0;

  // 顶部独立底栏，避免数字压在柱子上。
  ctx.fillStyle = "rgba(7, 13, 20, 0.94)";
  ctx.fillRect(left, 0, domWidth, headerH);
  ctx.strokeStyle = "#1b2836";
  ctx.beginPath();
  ctx.moveTo(left, headerH + 0.5);
  ctx.lineTo(left + domWidth, headerH + 0.5);
  ctx.stroke();
  ctx.fillStyle = "#e2e8f0";
  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "top";
  ctx.fillText(`max ${fmtNotional(maxValue)}`, left + 4, 4);
  ctx.fillStyle = "#86efac";
  ctx.fillText(`买 ${bidPct.toFixed(0)}%`, left + 4, 18);
  ctx.fillStyle = "#fca5a5";
  ctx.fillText(`卖 ${askPct.toFixed(0)}%`, left + 58, 18);
}

function drawPriceAxis(
  width,
  plotRight,
  plotBottom,
  rowHeight,
  bucketToY,
  topBucket,
  bottomBucket,
  domWidth,
  axisWidth
) {
  const left = plotRight + domWidth;
  ctx.fillStyle = "#0a1017";
  ctx.fillRect(left, 0, axisWidth, plotBottom);
  ctx.strokeStyle = "#1b2836";
  ctx.beginPath();
  ctx.moveTo(left + 0.5, 0);
  ctx.lineTo(left + 0.5, plotBottom);
  ctx.stroke();

  const step = Math.max(1, Math.ceil(34 / rowHeight));
  ctx.fillStyle = "#7f95a8";
  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "middle";
  // 稀疏百分比网格可能跨过很多空 bucket；直接按标签步长跳跃，避免 60 FPS
  // 下逐个扫描不可见空档。
  const firstBucket = Math.ceil(bottomBucket / step) * step;
  for (let bucket = firstBucket; bucket <= topBucket; bucket += step) {
    const price = bucketToPrice(bucket);
    if (price === null) continue;
    const y = bucketToY(bucket) - rowHeight / 2;
    if (y < 8 || y > plotBottom - 4) continue;
    ctx.fillText(fmtPrice(price), left + 6, y);
    ctx.fillStyle = "rgba(93,113,131,0.07)";
    ctx.fillRect(0, y, plotRight, 1);
    ctx.fillStyle = "#7f95a8";
  }
}

/*
 * 价轴上的当前盘口标签：卖一红、买一绿，和主图两条阶梯线同色，
 * 标签覆盖整条价轴宽度，避免压在刻度上留下半截数字。
 */
function drawPriceTags(width, plotRight, bucketToY, axisWidth) {
  const dom = state.dom;
  const rowHalf = viewport.rowHeight / 2;
  const tags = [];
  const metrics = currentDomMetrics();
  if (dom && dom.ready && metrics.askCount && metrics.bidCount) {
    tags.push(["ask", metrics.bestAskLower, "#f85a50", "#20060a"]);
    tags.push(["bid", metrics.bestBidUpper, "#2dd4a0", "#04140f"]);
  } else if (state.mid !== null) {
    tags.push(["mid", state.mid, "#e2e8f0", "#04070b"]);
  }

  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "middle";
  const placed = [];
  for (const [, price, background, textColor] of tags) {
    if (!Number.isFinite(price) || price <= 0) continue;
    const bucket = priceToBucket(price);
    if (bucket === null) continue;
    let y = bucketToY(bucket) - rowHalf;
    // 买一/卖一贴太近时错开，避免两个标签叠在一起看不清。
    for (const other of placed) {
      if (Math.abs(y - other) < 15) y = other + (y >= other ? 15 : -15);
    }
    placed.push(y);
    ctx.fillStyle = background;
    ctx.fillRect(width - axisWidth, y - 8, axisWidth, 16);
    ctx.fillStyle = textColor;
    ctx.fillText(fmtPrice(price), width - axisWidth + 6, y);
  }
}

/* ------------------------------------------------------------------ 交互 */

function canvasLayoutMetrics(rect) {
  const width = rect.width;
  const height = rect.height;
  const { axisWidth, domWidth } = chartSideWidths(width);
  const plotRight = Math.max(0, width - axisWidth - domWidth);
  const plotBottom = Math.max(1, height - PLOT_BOTTOM);
  return { width, height, plotRight, plotBottom, axisWidth, domWidth };
}

function hitRegionAt(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const { plotRight, plotBottom } = canvasLayoutMetrics(rect);
  // 价轴与右侧 DOM 共价坐标 → 价格缩放；热图 + 底边时间轴 → 时间横轴缩放。
  if (x >= plotRight && y <= plotBottom) return "price";
  if (x < plotRight) return "time";
  return "none";
}

function touchRegionAt(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const { width, plotBottom, axisWidth } = canvasLayoutMetrics(rect);
  // 手机只把最右侧真实价轴作为缩放命中区；DOM 仍属于可拖动主图，避免
  // 用户查看柱值时误缩放。68px 紧凑价轴也高于 44px 触控可达下限。
  if (x >= width - axisWidth && y <= plotBottom) return "price-axis";
  if (y <= plotBottom) return "price-pan";
  return "none";
}

function zoomTimeAxis(deltaY) {
  viewport.colWidth = Math.max(
    minTimeColumnWidth(),
    Math.min(14, viewport.colWidth * (deltaY > 0 ? 0.88 : 1.14))
  );
  markUiRevision();
  // 实时模式只缩放当前十分钟会话；复盘模式才合并调度磁盘历史。
  requestRender();
  if (state.appMode === APP_MODE_REPLAY) {
    scheduleHistoryForViewport(HISTORY_REQUEST_DEBOUNCE_MS);
  }
}

function minTimeColumnWidth() {
  const rect = canvas.getBoundingClientRect();
  const { axisWidth, domWidth } = chartSideWidths(rect.width);
  const plotRight = Math.max(0, rect.width - axisWidth - domWidth);
  const liveX = timelineGeometry(plotRight, viewport.colWidth, 0).liveX;
  return Math.max(
    MIN_TIME_COL_WIDTH,
    (liveX * columnIntervalMs()) / activeMaxLookbackMs()
  );
}

function renderedPriceCoverageBase() {
  return clampCoverageTarget(
    Number.isFinite(viewport.renderedCoveragePct)
      ? viewport.renderedCoveragePct
      : viewport.coveragePct
  );
}

function enterManualPriceScale() {
  if (!viewport.autoFitBook) return false;
  // 第一次人工操作必须从眼前的真实画面接管，不能从隐藏的 5%
  // 目标起算，否则手势刚开始画面就会大幅跳变。
  viewport.coveragePct = renderedPriceCoverageBase();
  viewport.autoFitBook = false;
  autoBookViewport.refitPending = false;
  viewport.priceScaleMode = "manual";
  return true;
}

function setPriceCoverage(candidate, previous = renderedPriceCoverageBase()) {
  const modeChanged = enterManualPriceScale();
  const next = clampCoverageTarget(candidate);
  if (Math.abs(next - previous) < 1e-12 && !modeChanged) return false;
  viewport.coveragePct = next;
  markUiRevision();
  requestRender();
  return true;
}

function zoomPriceAxis(deltaY) {
  const factor = deltaY > 0 ? 1.12 : 0.89;
  // 先从当前锁定视窗起算：新币默认目标虽为 5%，薄盘口画面
  // 可能只有 0.2%；第一格滚轮必须无缝接管 0.2%。
  const prev = renderedPriceCoverageBase();
  // 连续目标必须累积，即便本次还没跨过一个离散 bucket，也不能 snap 回
  // 当前整数档覆盖；否则粗网格会表现为滚轮失效。
  setPriceCoverage(prev * factor, prev);
}

function zoomPriceAxisFromTouch(startCoverage, deltaY) {
  // 始终从手势起点计算，约 160px 改变一倍；不受设备 pointermove 频率影响。
  const base = clampCoverageTarget(startCoverage);
  setPriceCoverage(base * Math.pow(2, deltaY / 160), clampCoverageTarget(viewport.coveragePct));
}

function panPriceAxisByPixels(deltaY) {
  if (viewport.centerBucket === null) return;
  enterManualPriceScale();
  viewport.centerBucket += deltaY / viewport.rowHeight;
  viewport.follow = false;
  el("followToggle").checked = false;
  markUiRevision();
  requestRender();
}

function enablePriceFollow() {
  resetEdgeAutoRecenterState();
  viewport.follow = true;
  el("followToggle").checked = true;
  markUiRevision();
  requestRender();
}

function enableBookAutoFit() {
  // 右侧价轴双击/双点是唯一会主动重新读取当前盘口边界的操作。
  // 恢复 5% 产品上限和 mid 跟随，然后于下一帧建立新的稳定锁定。
  viewport.coveragePct = DEFAULT_PRICE_COVERAGE_PCT;
  viewport.autoFitBook = true;
  viewport.follow = true;
  // 用户曾手动平移时 centerBucket 可能远离当前 mid。重新适配必须先
  // 回到当前 mid，否则会以旧中心计算过大范围并永久锁错。
  viewport.centerBucket = null;
  viewport.centerFrameAt = 0;
  resetEdgeAutoRecenterState();
  requestAutoBookViewportRefit(viewport.renderedCoveragePct);
  el("followToggle").checked = true;
  markUiRevision();
  requestRender();
}

function trimBubblesToActiveWindow(referenceT) {
  const cutoff = Number(referenceT) - activeMaxLookbackMs() - 5000;
  const expired = lowerBoundBubbleBucketTime(state.bubbles, cutoff);
  if (expired > 0) {
    removeBubblePrefix(expired);
    state.bubbleRevision += 1;
  }
}

function syncModeControls() {
  const replay = state.appMode === APP_MODE_REPLAY;
  const modeSelect = el("appMode");
  modeSelect.value = state.appMode;
  modeSelect.disabled = !replay && !hasFirstFrame;
  el("targetSelect").disabled = replay;
  el("symbolSelect").disabled = replay;
  el("symbolInput").disabled = replay;
  el("switchButton").disabled = replay;
  el("continuousHeatmapToggle").disabled = replay;
  el("tasPane").classList.toggle("hidden", replay);
  modeSelect.title = replay && state.replaySessionLimited
    ? "当前服务没有可用的逐条成交历史，只能冻结当前页面会话，最多10分钟。"
    : "实时固定10分钟；具备逐条历史能力时静态复盘固定10分钟。";
}

function stopLiveConnectionForReplay() {
  liveConnectionWanted = false;
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  socketGeneration += 1;
  const activeSocket = socket;
  socket = null;
  if (
    activeSocket &&
    activeSocket.readyState !== WebSocket.CLOSING &&
    activeSocket.readyState !== WebSocket.CLOSED
  ) {
    activeSocket.close(1000, "static replay");
  }
  transportConnected = false;
  liveBookFresh = false;
}

function enterReplayMode() {
  if (state.appMode === APP_MODE_REPLAY) return true;
  const newestLive = state.columns.length
    ? Number(state.columns[state.columns.length - 1].t)
    : NaN;
  if (!hasFirstFrame || !Number.isFinite(newestLive) || !gridIdentity(state.view)) {
    el("appMode").value = APP_MODE_LIVE;
    syncPageUrlState({ mode: APP_MODE_LIVE });
    el("gapNotice").textContent = "请等待实时首帧到达后再进入静态复盘";
    el("gapNotice").classList.remove("hidden");
    window.setTimeout(() => el("gapNotice").classList.add("hidden"), 3000);
    return false;
  }

  const historyAvailable = rawTradeHistoryAvailable(state.view);
  cancelHistoryLoad();
  state.appMode = APP_MODE_REPLAY;
  resetEdgeAutoRecenterState();
  state.replayAsOfMs = newestLive;
  state.replaySessionLimited = !historyAvailable;
  state.historyLookbackMs = activeMaxLookbackMs();
  state.historyStatus = historyAvailable
    ? "idle"
    : state.tradeHistoryTruncated
      ? "partial"
      : "session-only";
  // 复盘纵轴由冻结历史自身决定，不得沿用实时 20 档可见盘口的薄边界。
  lastTrustedBookBounds = null;
  resetAutoBookViewport(false);
  viewport.autoFitBook = false;
  viewport.coveragePct = DEFAULT_PRICE_COVERAGE_PCT;
  viewport.renderedCoveragePct = DEFAULT_PRICE_COVERAGE_PCT;
  viewport.renderedBookCoveragePct = null;
  viewport.bookOutsideViewport = false;
  viewport.priceScaleMode = "manual";
  stopLiveConnectionForReplay();

  // 只有明确声明逐条历史能力的新服务才保留 3 分钟实时尾段并让磁盘接管；
  // 旧进程即使 historyAvailable=true 也保留完整十分钟会话，绝不伪造磁盘历史。
  const liveTailMs = historyAvailable ? windowMs() : LIVE_MAX_LOOKBACK_MS;
  const liveCutoff = newestLive - liveTailMs;
  state.columns = state.columns.filter(
    (column) => !column.history && Number(column.t) >= liveCutoff
  );
  state.bubbles = state.bubbles.filter(
    (bubble) => !bubble.history && bubbleEventTimeMs(bubble) >= liveCutoff
  );
  rebuildBubbleTradeIds();
  state.historyColumns = 0;
  state.heatmapRevision += 1;
  state.bubbleRevision += 1;
  state.dom = null;
  state.mid = null;
  state.lastTrade = null;
  state.status = null;
  state.domRevision += 1;
  state.trades = [];
  el("tasList").textContent = "";
  viewport.follow = false;
  viewport.centerBucket = null;
  el("followToggle").checked = false;
  anchorVisualClockToNewest();
  markUiRevision();
  invalidateHeatmapCache();
  invalidateLiveBookCache();
  syncModeControls();
  syncPageUrlState();
  updateHeader();
  requestRender();

  if (historyAvailable) {
    scheduleHistoryForViewport(0);
    if (activeTradeMinNotional() < activeTradeQueryMinNotional()) {
      showHistoryNotice(
        `主图不过滤；静态复盘受归档底线限制，仅含单笔 ≥ ${fmtNotional(
          activeTradeQueryMinNotional()
        )} 的事件`
      );
    }
  } else {
    showHistoryNotice(state.tradeHistoryTruncated
      ? `当前服务无逐条成交历史，且本页会话已达 ${MAX_COMBINED_BUBBLES.toLocaleString(
          "zh-CN"
        )} 条上限：最早成交已缺失`
      : "当前服务未声明逐条成交历史能力：仅冻结当前会话，范围上限10分钟");
  }
  return true;
}

function maybeEnterInitialReplayMode() {
  if (
    !pendingInitialReplayMode ||
    state.appMode !== APP_MODE_LIVE ||
    !hasFirstFrame ||
    !gridIdentity(state.view)
  ) {
    return false;
  }
  pendingInitialReplayMode = false;
  return enterReplayMode();
}

function leaveReplayMode() {
  if (state.appMode !== APP_MODE_REPLAY) return;
  const target = state.target || el("targetSelect").value || DEFAULT_TARGET;
  const symbol = state.symbol || el("symbolInput").value.trim().toUpperCase();
  cancelHistoryLoad();
  state.appMode = APP_MODE_LIVE;
  resetEdgeAutoRecenterState();
  state.replayAsOfMs = null;
  state.replaySessionLimited = false;
  state.historyLookbackMs = LIVE_MAX_LOOKBACK_MS;
  liveConnectionWanted = true;
  hasFirstFrame = false;
  // 从冻结历史回到实时时必须等待新 DOM 首帧重建锁定，不能复用
  // 进入复盘前的薄盘口范围。
  viewport.coveragePct = DEFAULT_PRICE_COVERAGE_PCT;
  viewport.autoFitBook = true;
  viewport.follow = true;
  viewport.centerBucket = null;
  viewport.centerFrameAt = 0;
  viewport.renderedCoveragePct = DEFAULT_PRICE_COVERAGE_PCT;
  viewport.renderedBookCoveragePct = null;
  viewport.bookOutsideViewport = false;
  viewport.priceScaleMode = "pending";
  el("followToggle").checked = true;
  resetAutoBookViewport(false);
  resetStream(-1, target, symbol);
  state.subscriptionEpoch = -1;
  state.meta = null;
  state.view = null;
  state.status = null;
  state.dom = null;
  state.mid = null;
  state.lastTrade = null;
  viewport.colWidth = Math.max(viewport.colWidth, minTimeColumnWidth());
  el("historyNotice").classList.add("hidden");
  markUiRevision();
  syncModeControls();
  syncPageUrlState();
  updateHeader();
  requestRender();
  connect();
}

el("appMode").addEventListener("change", (event) => {
  pendingInitialReplayMode = false;
  if (event.target.value === APP_MODE_REPLAY) {
    enterReplayMode();
  } else {
    leaveReplayMode();
  }
});

canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  const region = hitRegionAt(event.clientX, event.clientY);
  // Shift 反转：热图上也可临时调纵轴，价轴上也可临时调横轴。
  const wantPrice =
    region === "price" ? !event.shiftKey : region === "time" && event.shiftKey;
  const wantTime =
    region === "time" ? !event.shiftKey : region === "price" && event.shiftKey;
  if (wantPrice) {
    zoomPriceAxis(event.deltaY);
    return;
  }
  if (wantTime) {
    zoomTimeAxis(event.deltaY);
  }
}, { passive: false });

canvas.addEventListener("mousemove", (event) => {
  if (viewport.dragging) return;
  const region = hitRegionAt(event.clientX, event.clientY);
  canvas.style.cursor =
    region === "price" ? "ns-resize" : region === "time" ? "ew-resize" : "default";
});

canvas.addEventListener("mousedown", (event) => {
  if (event.button !== 0) return;
  viewport.dragging = true;
  viewport.lastY = event.clientY;
});

window.addEventListener("mousemove", (event) => {
  if (!viewport.dragging) return;
  const delta = event.clientY - viewport.lastY;
  viewport.lastY = event.clientY;
  panPriceAxisByPixels(delta);
});

window.addEventListener("mouseup", () => {
  viewport.dragging = false;
});

canvas.addEventListener("dblclick", (event) => {
  if (hitRegionAt(event.clientX, event.clientY) === "price") {
    enableBookAutoFit();
  } else {
    enablePriceFollow();
  }
});

const touchGesture = {
  pointerId: null,
  mode: "none",
  startX: 0,
  startY: 0,
  lastY: 0,
  startCoverage: DEFAULT_PRICE_COVERAGE_PCT,
  maxDistance: 0,
  lastTapAt: 0,
  lastTapMode: "none",
};

function finishTouchGesture(event, allowTap) {
  if (event.pointerId !== touchGesture.pointerId) return;
  if (
    allowTap &&
    (touchGesture.mode === "price-pan" || touchGesture.mode === "price-axis") &&
    touchGesture.maxDistance < 8
  ) {
    const current = performance.now();
    if (
      touchGesture.mode === touchGesture.lastTapMode &&
      current - touchGesture.lastTapAt <= 340
    ) {
      if (touchGesture.mode === "price-axis") enableBookAutoFit();
      else enablePriceFollow();
      touchGesture.lastTapAt = 0;
      touchGesture.lastTapMode = "none";
    } else {
      touchGesture.lastTapAt = current;
      touchGesture.lastTapMode = touchGesture.mode;
    }
  }
  if (
    typeof canvas.hasPointerCapture === "function" &&
    canvas.hasPointerCapture(event.pointerId)
  ) {
    canvas.releasePointerCapture(event.pointerId);
  }
  touchGesture.pointerId = null;
  touchGesture.mode = "none";
  viewport.dragging = false;
}

canvas.addEventListener("pointerdown", (event) => {
  if (event.pointerType !== "touch" || touchGesture.pointerId !== null) return;
  const mode = touchRegionAt(event.clientX, event.clientY);
  if (mode === "none") return;
  event.preventDefault();
  touchGesture.pointerId = event.pointerId;
  touchGesture.mode = mode;
  touchGesture.startX = event.clientX;
  touchGesture.startY = event.clientY;
  touchGesture.lastY = event.clientY;
  touchGesture.startCoverage = renderedPriceCoverageBase();
  touchGesture.maxDistance = 0;
  viewport.dragging = true;
  if (typeof canvas.setPointerCapture === "function") {
    canvas.setPointerCapture(event.pointerId);
  }
}, { passive: false });

canvas.addEventListener("pointermove", (event) => {
  if (event.pointerId !== touchGesture.pointerId) return;
  event.preventDefault();
  touchGesture.maxDistance = Math.max(
    touchGesture.maxDistance,
    Math.hypot(
      event.clientX - touchGesture.startX,
      event.clientY - touchGesture.startY
    )
  );
  // 轻点时手指通常会有 2–5px 微抖。在越过 tap 死区前不得接管
  // 相机，否则一次普通单点也会意外关闭 auto-fit/follow。
  if (touchGesture.maxDistance < 8) return;
  if (touchGesture.mode === "price-axis") {
    zoomPriceAxisFromTouch(
      touchGesture.startCoverage,
      event.clientY - touchGesture.startY
    );
    return;
  }
  const delta = event.clientY - touchGesture.lastY;
  touchGesture.lastY = event.clientY;
  panPriceAxisByPixels(delta);
}, { passive: false });

canvas.addEventListener("pointerup", (event) => finishTouchGesture(event, true));
canvas.addEventListener("pointercancel", (event) => finishTouchGesture(event, false));
canvas.addEventListener("lostpointercapture", (event) => {
  if (event.pointerId === touchGesture.pointerId) {
    touchGesture.pointerId = null;
    touchGesture.mode = "none";
    viewport.dragging = false;
  }
});

el("followToggle").addEventListener("change", (event) => {
  resetEdgeAutoRecenterState();
  viewport.follow = event.target.checked;
  markUiRevision();
  requestRender();
});

el("continuousHeatmapToggle").addEventListener("change", (event) => {
  ui.continuousHeatmap = event.target.checked;
  savePreference("continuousHeatmap", ui.continuousHeatmap);
  invalidateHeatmapCache();
  markUiRevision();
  requestRender();
});

el("tasFilter").addEventListener("change", (event) => {
  ui.tasThreshold = Number(event.target.value);
  renderTas();
});

el("tasPause").addEventListener("click", () => {
  ui.paused = !ui.paused;
  el("tasPause").textContent = ui.paused ? "跟随" : "暂停";
  el("tasMeta").textContent = ui.paused
    ? "已暂停，新增成交不再写入列表"
    : "有界列表，仅内存";
  if (!ui.paused) renderTas();
});

el("colorMode").addEventListener("change", (event) => {
  ui.colorMode = event.target.value;
  savePreference("colorMode", ui.colorMode);
  syncLegend();
  markUiRevision();
  requestRender();
});

el("heatContrast").addEventListener("input", (event) => {
  scheduleHeatContrast(event.target.value);
});

el("heatContrast").addEventListener("change", (event) => {
  if (contrastRenderTimer !== null) {
    window.clearTimeout(contrastRenderTimer);
    contrastRenderTimer = null;
  }
  applyHeatContrast(event.target.value, true);
});

el("bubbleScale").addEventListener("change", (event) => {
  ui.bubbleScale = Number(event.target.value) || 1;
  savePreference("bubbleScale", ui.bubbleScale);
  markUiRevision();
  requestRender();
});

el("bubbleFloor").addEventListener("change", (event) => {
  const preference = event.target.value;
  const previousFloor = activeTradeMinNotional();
  const previousQueryFloor = activeTradeQueryMinNotional();
  applyBubbleFloorPreference(preference);
  savePreference("bubbleFloor", preference);
  const nextFloor = activeTradeMinNotional();
  const nextQueryFloor = activeTradeQueryMinNotional();
  if (nextFloor !== previousFloor) {
    // 原始单笔 reservoir 与显示门槛完全解耦。提高门槛只触发这一帧本地显隐；
    // 降低到尚未覆盖的归档门槛时，后台只补逐条成交，不清盘口、不清旧气泡。
    // 唯一例外是低门槛已命中 100k 硬帽：此时提高门槛必须按新门槛补回
    // 可能被旧前缀淘汰的大单，不能拿 partial reservoir 冒充完整本地过滤。
    state.bubbleRevision += 1;
    if (state.appMode === APP_MODE_REPLAY) {
      const replayIdentity = currentHistoryIdentity();
      const higherFloorCoverageMissing = Boolean(
        nextQueryFloor > previousQueryFloor &&
        replayIdentity &&
        missingTradeHistoryRanges(
          nextQueryFloor,
          replayIdentity.asOfMs - activeMaxLookbackMs(),
          replayIdentity.asOfMs
        ).length
      );
      const recoverHigherFloorHistory =
        nextQueryFloor > previousQueryFloor &&
        prepareHigherFloorTradeRecovery(
          nextQueryFloor,
          higherFloorCoverageMissing
        );
      const staleLowerFloorInFlight = Boolean(
        nextQueryFloor > previousQueryFloor &&
        historyInFlightKind === "trades" &&
        Number.isFinite(Number(historyInFlightTradeFloor)) &&
        Number(historyInFlightTradeFloor) < Number(nextQueryFloor)
      );
      const higherFloorNeedsRepair =
        recoverHigherFloorHistory ||
        higherFloorCoverageMissing ||
        staleLowerFloorInFlight;
      if (nextQueryFloor < previousQueryFloor || higherFloorNeedsRepair) {
        scheduleHistoryForViewport(0, "trades");
      }
      if (nextFloor < nextQueryFloor) {
        showHistoryNotice(
          `主图不过滤；静态复盘受归档底线限制，仅含单笔 ≥ ${fmtNotional(
            nextQueryFloor
          )} 的事件`
        );
      }
    } else {
      showHistoryNotice(nextFloor > 0
        ? `单笔门槛已切换为 ≥ ${fmtNotional(nextFloor)}；最近10分钟原始单笔已在本地显隐`
        : `主图过滤已关闭；实时显示本页保留的全部单笔，静态复盘仍受 ≥ ${fmtNotional(
            archiveMinNotional()
          )} 归档底线限制`);
    }
  }
  syncDensityControl();
  markUiRevision();
  requestRender();
});

el("switchButton").addEventListener("click", () => {
  if (state.appMode !== APP_MODE_LIVE) return;
  const target = selectedTarget();
  const symbol = canonicalSymbolForTarget(target, el("symbolInput").value);
  if (!symbol) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    el("gapNotice").textContent = "切换失败：页面尚未连接服务";
    el("gapNotice").classList.remove("hidden");
    setTimeout(() => el("gapNotice").classList.add("hidden"), 3000);
    return;
  }
  cancelHistoryLoad();
  hasFirstFrame = false;
  syncModeControls();
  socket.send(
    JSON.stringify({
      type: "subscribe",
      target,
      symbol,
      minNotional: activeTradeMinNotional(),
    })
  );
  setBadge("SWITCHING", "waiting");
  el("feedStatus").textContent = `正在为本页面订阅 ${targetLabel(target)} · ${symbol}`;
});

el("targetSelect").addEventListener("change", (event) => {
  if (state.appMode !== APP_MODE_LIVE) return;
  const target = normalizedTarget(event.target.value) || DEFAULT_TARGET;
  populateSymbolList(target);
  const preferred = preferredSymbolForTarget(target);
  if (preferred) el("symbolInput").value = preferred;
  syncTargetPresentation(target);
});

el("symbolInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === "Return" || event.keyCode === 13) {
    el("switchButton").click();
  }
});

el("symbolSelect").addEventListener("change", (event) => {
  if (state.appMode !== APP_MODE_LIVE || !event.target.value) return;
  el("symbolInput").value = event.target.value;
});

el("symbolInput").addEventListener("input", syncSymbolSelection);

async function loadSymbols() {
  try {
    const response = await fetch(appPath("/api/symbols"), { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    const targets = Array.isArray(payload && payload.targets)
      ? payload.targets
      : [{
          id: DEFAULT_TARGET,
          label: "沙盒交易所",
          symbols: Array.isArray(payload && payload.symbols) ? payload.symbols : [],
        }];
    for (const entry of targets) {
      const id = normalizedTarget(entry && entry.id);
      if (!SUPPORTED_TARGETS.includes(id)) continue;
      const current = targetCatalog.get(id) || { id, label: id, symbols: [] };
      const symbols = Array.isArray(entry && entry.symbols)
        ? entry.symbols
            .filter((symbol) => typeof symbol === "string" && symbol.trim())

        : [];
      targetCatalog.set(id, {
        id,
        label: String(entry && entry.label || current.label || id),
        symbols,
      });
    }
    rebuildTargetOptions();
    populateSymbolList(selectedTarget(), { restore: false });
    syncTargetPresentation();
  } catch (error) {
    /* 合约目录不可用时仍允许手动输入 */
  }
}

function symbolPreferenceKey(target) {
  return `symbol.${normalizedTarget(target) || DEFAULT_TARGET}`;
}

function selectedTarget() {
  const candidate = normalizedTarget(el("targetSelect").value);
  return SUPPORTED_TARGETS.includes(candidate) ? candidate : DEFAULT_TARGET;
}

function targetLabel(target = selectedTarget()) {
  const id = normalizedTarget(target) || DEFAULT_TARGET;
  const entry = targetCatalog.get(id);
  return entry ? entry.label : id;
}

function canonicalSymbolForTarget(target, value) {
  const raw = String(value || "").trim();
  if (!raw || !/^[A-Za-z0-9_.:/-]{1,64}$/.test(raw)) return "";
  const entry = targetCatalog.get(normalizedTarget(target));
  const match = entry && entry.symbols.find(
    (symbol) => normalizedHistorySymbol(symbol) === normalizedHistorySymbol(raw)
  );
  if (match) return match;
  return normalizedTarget(target) === "sandbox" ? raw.toUpperCase() : raw;
}

function safePageSymbol(value) {
  const raw = String(value || "").trim();
  return raw && /^[A-Za-z0-9_.:/-]{1,64}$/.test(raw) ? raw : "";
}

function normalizedPageMode(value) {
  return value === APP_MODE_REPLAY ? APP_MODE_REPLAY : APP_MODE_LIVE;
}

function readPageUrlState(params = PAGE_PARAMS) {
  const hasTarget = params.has("target");
  const requestedTarget = normalizedTarget(params.get("target"));
  const targetIsValid = SUPPORTED_TARGETS.includes(requestedTarget);
  return {
    // 显式但无效的 target 确定性回退 Binance；没有 target 才继续沿用
    // 旧版 localStorage 偏好，保证旧的裸 URL 行为不变。
    target: hasTarget
      ? targetIsValid
        ? requestedTarget
        : DEFAULT_TARGET
      : "",
    // target 显式无效时不能把它携带的 symbol 偷偷解释为 Binance 身份。
    symbol: !hasTarget || targetIsValid
      ? safePageSymbol(params.get("symbol"))
      : "",
    mode: normalizedPageMode(params.get("mode")),
  };
}

function buildPageUrlSearch(
  target,
  symbol,
  mode,
  sourceParams = PAGE_PARAMS
) {
  const params = new URLSearchParams();
  const requestedTarget = normalizedTarget(target);
  params.set(
    "target",
    SUPPORTED_TARGETS.includes(requestedTarget)
      ? requestedTarget
      : DEFAULT_TARGET
  );
  params.set("symbol", safePageSymbol(symbol));
  params.set("mode", normalizedPageMode(mode));
  for (const key of PAGE_URL_PASSTHROUGH_KEYS) {
    if (sourceParams.has(key)) params.set(key, sourceParams.get(key) || "");
  }
  return params.toString();
}

function currentPageUrlMode() {
  return pendingInitialReplayMode ? APP_MODE_REPLAY : state.appMode;
}

function syncPageUrlState(overrides = {}) {
  const requestedTarget = normalizedTarget(
    overrides.target || state.target || selectedTarget()
  );
  const target = SUPPORTED_TARGETS.includes(requestedTarget)
    ? requestedTarget
    : DEFAULT_TARGET;
  const symbol =
    canonicalSymbolForTarget(
      target,
      overrides.symbol || state.symbol || el("symbolInput").value
    ) ||
    preferredSymbolForTarget(target) ||
    (target === DEFAULT_TARGET ? "BTCUSDT" : "");
  const mode = normalizedPageMode(overrides.mode || currentPageUrlMode());
  const search = buildPageUrlSearch(target, symbol, mode);
  const nextRelative = `${location.pathname}?${search}${location.hash || ""}`;
  const currentRelative = `${location.pathname}${location.search}${location.hash || ""}`;
  if (nextRelative !== currentRelative) {
    // 地址始终可复制，但每次切币/切模式不污染浏览器后退栈。
    window.history.replaceState(window.history.state, "", nextRelative);
  }
}

function preferredSymbolForTarget(target) {
  const id = normalizedTarget(target) || DEFAULT_TARGET;
  const entry = targetCatalog.get(id);
  const catalogFallback = entry && entry.symbols.length ? entry.symbols[0] : "";
  const legacyFallback = id === DEFAULT_TARGET
    ? loadPreference("symbol", "BTCUSDT")
    : catalogFallback;
  return canonicalSymbolForTarget(
    id,
    loadPreference(symbolPreferenceKey(id), legacyFallback)
  ) || catalogFallback;
}

function populateSymbolList(target, { restore = true } = {}) {
  const id = normalizedTarget(target) || DEFAULT_TARGET;
  const entry = targetCatalog.get(id);
  const list = el("symbolList");
  const select = el("symbolSelect");
  const symbols = entry ? entry.symbols : [];
  const catalogKey = JSON.stringify([id, symbols, [...enabledMakerSymbols].sort()]);
  // The native datalist filters by the input's current text (BTCUSDT excludes
  // BTC11USDT-PERP). Keep a separate, complete directory available at all times.
  // Do not rebuild an unchanged native dropdown every ten seconds while open.
  if (select.dataset.catalogKey !== catalogKey) {
    select.dataset.catalogKey = catalogKey;
    list.textContent = "";
    select.textContent = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = symbols.length ? `全部币对（${symbols.length}）` : "正在加载币对";
    select.append(placeholder);
    for (const symbol of symbols) {
      const option = document.createElement("option");
      option.value = symbol;
      option.textContent = symbol + (enabledMakerSymbols.has(symbol) ? " · LIVE" : "");
      select.append(option);
      list.append(option.cloneNode(true));
    }
  }
  if (restore) {
    const preferred = preferredSymbolForTarget(id);
    if (preferred) el("symbolInput").value = preferred;
  }
  syncSymbolSelection();
}

function syncSymbolSelection() {
  const symbol = canonicalSymbolForTarget(selectedTarget(), el("symbolInput").value);
  const entry = targetCatalog.get(selectedTarget());
  el("symbolSelect").value = entry && entry.symbols.includes(symbol) ? symbol : "";
}

function rebuildTargetOptions() {
  const select = el("targetSelect");
  const selected = selectedTarget();
  select.textContent = "";
  for (const id of SUPPORTED_TARGETS) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = targetLabel(id);
    select.append(option);
  }
  select.value = SUPPORTED_TARGETS.includes(selected) ? selected : DEFAULT_TARGET;
}

function syncTargetPresentation(target = selectedTarget()) {
  const label = targetLabel(target);
  el("marketLabel").textContent = `${label} · 只读`;
  el("symbolInput").title = `输入 ${label} 的币对代码筛选候选，或从左侧全部币对选择`;
}

function restorePreferences() {
  const pageState = readPageUrlState();
  const storedTarget = loadPreference("target", DEFAULT_TARGET, SUPPORTED_TARGETS);
  const preferredTarget = pageState.target || storedTarget;
  el("targetSelect").value = preferredTarget;
  state.target = preferredTarget;
  const preferredSymbol =
    canonicalSymbolForTarget(preferredTarget, pageState.symbol) ||
    preferredSymbolForTarget(preferredTarget) ||
    "BTCUSDT";
  el("symbolInput").value = preferredSymbol;
  state.symbol = preferredSymbol;
  populateSymbolList(preferredTarget, { restore: false });
  syncTargetPresentation(preferredTarget);
  ui.colorMode = loadPreference("colorMode", "heatmap", Object.keys(PALETTES));
  el("colorMode").value = ui.colorMode;
  ui.heatContrast = normalizeHeatContrast(
    loadPreference("heatContrast", "0")
  );
  syncHeatContrastControl();
  ui.continuousHeatmap =
    loadPreference("continuousHeatmap", "true", ["true", "false"]) === "true";
  el("continuousHeatmapToggle").checked = ui.continuousHeatmap;
  ui.bubbleScale = Number(loadPreference("bubbleScale", "1.9")) || 1.9;
  el("bubbleScale").value = String(ui.bubbleScale);
  let bubbleFloorPreference = loadPreference("bubbleFloor", "fixed:5000");
  // 旧版自适应选项不具备固定单笔语义，统一迁移到轻量默认 5K；fixed:0
  // 是明确的实时不过滤，复盘查询仍由归档底线单独钳制。
  if (!DENSITY_OPTIONS.includes(bubbleFloorPreference)) {
    bubbleFloorPreference = "fixed:5000";
  }
  applyBubbleFloorPreference(bubbleFloorPreference);
  el("bubbleFloor").value = bubbleFloorPreference;
  pendingInitialReplayMode = pageState.mode === APP_MODE_REPLAY;
  syncPageUrlState({
    target: preferredTarget,
    symbol: preferredSymbol,
    mode: pageState.mode,
  });
}

function applyBubbleFloorPreference(preference) {
  const normalized = DENSITY_OPTIONS.includes(preference)
    ? preference
    : "fixed:5000";
  ui.bubbleFixedFloor = Number(normalized.slice(6));
}

function syncDensityControl() {
  const select = el("bubbleFloor");
  const displayFloor = activeTradeMinNotional();
  const queryFloor = activeTradeQueryMinNotional();
  select.title = displayFloor > 0
    ? `仅显示单条上游成交额不低于 ${fmtNotional(displayFloor)} 的事件；小单不会在本地合并后越过门槛。`
    : `实时主图不过滤；静态复盘受归档底线限制，只能查询单条成交额不低于 ${fmtNotional(queryFloor)} 的事件。`;
}

restorePreferences();
syncDensityControl();
syncLegend();
syncModeControls();
new ResizeObserver(resizeCanvas).observe(canvas.parentElement);
resizeCanvas();
connect();
loadSymbols();
setInterval(updateHeader, 1000);
document.addEventListener("visibilitychange", () => {
  nextVisualFrameAt = 0;
  if (!document.hidden) requestRender();
});

// 本地验收只读快照：不暴露可变 state 引用，不参与正式绘制路径。
function lmlDiagnostics() {
  const rect = canvas.getBoundingClientRect();
  const { axisWidth, domWidth } = chartSideWidths(rect.width);
  const domMetrics = currentDomMetrics();
  const plotRight = Math.max(1, rect.width - axisWidth - domWidth);
  const rawTimeShiftPx = visualTimeShiftPx();
  const timeShiftPx = alignedVisualShift(rawTimeShiftPx);
  const timeline = timelineGeometry(plotRight, viewport.colWidth, timeShiftPx);
  const historyCacheRight = timeline.liveX - timeShiftPx;
  // 可见历史由最后一列 sample-and-hold 到 NOW；当前盘口也严格从 NOW 开始。
  const historyRight = timeline.liveX;
  const liveBookLeft = timeline.liveX;
  const latestBubble = state.bubbles.length
    ? state.bubbles[state.bubbles.length - 1]
    : null;
  const latestBubbleX = latestBubble
    ? Math.min(
        timeline.liveX,
        historicalXForTime(
          bubbleEventTimeMs(latestBubble),
          timeline.liveX,
          viewport.colWidth,
          timeShiftPx
        )
      )
    : null;
  const historicalColumns = state.columns.filter((column) => column.history);
  const oldestHistoryT = historicalColumns.length
    ? Number(historicalColumns[0].t)
    : null;
  const newestHistoryT = historicalColumns.length
    ? Number(historicalColumns[historicalColumns.length - 1].t)
    : null;
  const diagnosticLiveCoverage = mergedLiveCoverageIntervals(state.columns);
  let compositeUncoveredPartialHistoryColumns = 0;
  let compositeUncoveredHardGapHistoryColumns = 0;
  for (let index = 0; index < state.columns.length; index += 1) {
    const column = state.columns[index];
    if (!column.history || (!column.partialCoverage && !column.gap)) continue;
    const interval = columnTimeInterval(state.columns, index);
    if (
      interval &&
      timeIntervalFullyCoveredByRanges(
        diagnosticLiveCoverage,
        interval.fromMs,
        interval.toMs
      )
    ) {
      continue;
    }
    if (column.gap) compositeUncoveredHardGapHistoryColumns += 1;
    else compositeUncoveredPartialHistoryColumns += 1;
  }
  const recentLiveDiscontinuities = [];
  let previousLiveDiagnostic = null;
  for (const column of state.columns) {
    if (column.history) continue;
    const timestamp = Number(column.t);
    const sequence = Number(column.seq);
    const previousT = previousLiveDiagnostic
      ? Number(previousLiveDiagnostic.t)
      : null;
    const previousSeq = previousLiveDiagnostic
      ? Number(previousLiveDiagnostic.seq)
      : null;
    const deltaMs = previousLiveDiagnostic ? timestamp - previousT : null;
    const sequenceDelta = previousLiveDiagnostic ? sequence - previousSeq : null;
    const levels = historyLevelCount(column.bids) + historyLevelCount(column.asks);
    if (
      column.gap ||
      levels <= 0 ||
      (previousLiveDiagnostic && (
        sequenceDelta !== 1 ||
        deltaMs > columnIntervalMs() + LIVE_VISUAL_JITTER_MS
      ))
    ) {
      recentLiveDiscontinuities.push({
        t: timestamp,
        seq: sequence,
        deltaMs,
        sequenceDelta,
        gap: Boolean(column.gap),
        levels,
      });
      if (recentLiveDiscontinuities.length > 24) {
        recentLiveDiscontinuities.shift();
      }
    }
    previousLiveDiagnostic = column;
  }
  return {
    mode: state.appMode,
    target: state.target,
    symbol: state.symbol,
    statusOverall:
      state.status && state.status.overall ? String(state.status.overall) : null,
    depthState:
      state.status && typeof state.status.depth === "string"
        ? state.status.depth
        : state.status && state.status.depth && state.status.depth.state
        ? String(state.status.depth.state)
        : null,
    depthReason:
      state.status && state.status.depthReason
        ? String(state.status.depthReason)
        : state.status && state.status.depth && state.status.depth.reason
        ? String(state.status.depth.reason)
        : null,
    depthReconnects:
      state.status
        ? Number(
            state.status.depthReconnects ||
              (state.status.depth && state.status.depth.reconnects) ||
              0
          )
        : 0,
    domReady: Boolean(state.dom && state.dom.ready),
    domBidRows: domMetrics.bidCount,
    domAskRows: domMetrics.askCount,
    gridEpoch:
      state.view && Number.isFinite(Number(state.view.gridEpoch))
        ? Number(state.view.gridEpoch)
        : null,
    asOf: state.replayAsOfMs,
    activeMaxLookbackMs: activeMaxLookbackMs(),
    tradeContract: rawTradeHistoryKind(state.view) || null,
    tradeSourceEventType: rawTradeSourceEventType(state.view) || null,
    tradeMinNotional: activeTradeMinNotional(),
    tradeDisplayMinNotional: activeTradeMinNotional(),
    tradeQueryMinNotional: activeTradeQueryMinNotional(),
    archiveMinNotional: archiveMinNotional(state.view),
    rawTradeHistoryKind:
      state.view && state.view.rawTradeHistoryKind
        ? String(state.view.rawTradeHistoryKind)
        : null,
    rawTradeHistoryAvailable: rawTradeHistoryAvailable(state.view),
    trackedTradeIds: bubbleTradeIds.size,
    rejectedTradeEvents,
    droppedTradeEvents: state.droppedTrades,
    bubbleRef: state.bubbleRef,
    bubbleSizeReference: state.bubbleSizeRef,
    bubbleSizeProfile: state.bubbleSizeProfile
      ? Object.assign({}, state.bubbleSizeProfile)
      : null,
    bubbleSizeSamples: state.bubbleSizeSamples.length,
    bubbleSizeProfileVersion: state.bubbleSizeProfileVersion,
    bubbleRadiusMaxPx: BUBBLE_RADIUS_DISPLAY_MAX_PX,
    bubbleSizingExamples: [5000, 15000, 60000].map((notional) => ({
      notional,
      radiusPx: bubbleRadiusPx(
        bubbleRadiusUnit(
          notional,
          state.bubbleSizeRef || BUBBLE_SIZE_COLD_START_NOTIONAL
        ),
        ui.bubbleScale
      ),
    })),
    bubbleSizingRatios: [0.1, 0.5, 1, 2, 10, 100].map((ratio) => ({
      ratio,
      radiusPx: bubbleRadiusPx(
        bubbleRadiusUnit(ratio, 1),
        ui.bubbleScale
      ),
    })),
    bubbleSample: state.bubbles.slice(0, 32).map((bubble) => ({
      key: bubble.tradeKey,
      id: bubble.id,
      sourceEventType: bubble.sourceEventType,
      applicationAggregation: bubble.applicationAggregation,
      notional: Number(bubble.notional),
      strength: bubble.displayStrength,
      sizeReference: bubble.displaySizeReference,
      sizeRatio: bubble.displaySizeRatio,
      sizeProfileVersion: bubble.displaySizeProfileVersion,
      radiusUnit: bubble.displayRadiusUnit,
      radiusPx: bubbleRadiusPx(bubble.displayRadiusUnit, ui.bubbleScale),
    })),
    bubbles: state.bubbles.length,
    lastBubbleVisited: bubbleRenderStats.lastVisited,
    lastBubbleRendered: bubbleRenderStats.lastRendered,
    lastBubbleVisibleFromMs: bubbleRenderStats.lastVisibleFromMs,
    bubbleDrawPoolSize: bubbleDrawItemPool.length,
    frozenBubbles: state.bubbles.filter((bubble) =>
      Number.isFinite(bubble.displayStrength)
    ).length,
    frozenHeatColumns: state.columns.filter(
      (column) =>
        column.gap ||
        (Number.isFinite(column.displayScaleLo) &&
          Number.isFinite(column.displayScaleHi))
    ).length,
    heatColumns: state.columns.length,
    historyColumns: state.historyColumns,
    partialHistoryColumns: state.columns.filter(
      (column) => column.history && column.partialCoverage
    ).length,
    hardGapHistoryColumns: state.columns.filter(
      (column) => column.history && column.gap
    ).length,
    compositeUncoveredPartialHistoryColumns,
    compositeUncoveredHardGapHistoryColumns,
    recentLiveDiscontinuities,
    historyStatus: state.historyStatus,
    historyVisibleLookbackMs: visibleHistoryLookbackMs(),
    historyPrefetchedLookbackMs: prefetchedHistoryLookbackMs(),
    historyMaxLookbackMs: activeMaxLookbackMs(),
    historyOldestT: oldestHistoryT,
    historyNewestT: newestHistoryT,
    // historyLoadedRanges 保留为只读兼容别名；新诊断明确拆成 book/trades。
    historyLoadedRanges: bookHistoryLoadedRanges.map((range) => ({
      fromMs: range.fromMs,
      toMs: range.toMs,
    })),
    bookHistoryLoadedRanges: bookHistoryLoadedRanges.map((range) => ({
      fromMs: range.fromMs,
      toMs: range.toMs,
    })),
    tradeHistoryCoverage: tradeHistoryCoverage.map((entry) => ({
      minNotional: entry.minNotional,
      ranges: entry.ranges.map((range) => ({
        fromMs: range.fromMs,
        toMs: range.toMs,
      })),
    })),
    tradeHistoryLowestLoadedFloor: tradeHistoryCoverage.length
      ? Math.min(...tradeHistoryCoverage.map((entry) => Number(entry.minNotional)))
      : null,
    tradeHistoryTruncated: state.tradeHistoryTruncated,
    tradeHistoryDropped: state.tradeHistoryDropped,
    historyInFlightKind,
    historyInFlightTradeFloor,
    historyInFlightRange: historyInFlightRange
      ? {
          fromMs: historyInFlightRange.fromMs,
          toMs: historyInFlightRange.toMs,
        }
      : null,
    historyLoadStats: Object.assign({}, historyLoadStats),
    cacheRebuilds: heatmapCache.rebuilds,
    cacheIncrementalUpdates: heatmapCache.incrementalUpdates,
    cacheSubpixelTailUpdates: heatmapCache.subpixelTailUpdates,
    lastPaintVisitedColumns: heatmapCache.lastPaintVisitedColumns,
    lastPaintRenderedColumns: heatmapCache.lastPaintRenderedColumns,
    temporalPixelSkips: heatmapCache.temporalPixelSkips,
    bookLinePixelSkips: heatmapCache.bookLinePixelSkips,
    cacheSeamCorrections: heatmapCache.seamCorrections,
    lastSeamCorrectionDevicePx: heatmapCache.lastSeamCorrectionDevicePx,
    maxSeamCorrectionDevicePx: heatmapCache.maxSeamCorrectionDevicePx,
    liveBookRebuilds: liveBookCache.rebuilds,
    visualFrames: heatmapCache.visualFrames,
    actualFps: frameStats.fps,
    lastRenderMs: frameStats.lastRenderMs,
    maxRenderMs: frameStats.maxRenderMs,
    skippedVisualFrames: frameStats.skippedVisualFrames,
    frameRateMode: "fixed",
    frameRateTarget: FIXED_FRAME_RATE,
    heatmapRevision: state.heatmapRevision,
    domRevision: state.domRevision,
    heatContrast: ui.heatContrast,
    continuousHeatmap: ui.continuousHeatmap,
    heatCutoffValue: heatCutoffValue(),
    pendingHeatContrast,
    coverageTargetPct: viewport.coveragePct,
    coverageRenderedPct: viewport.renderedCoveragePct,
    rawBookCoveragePct: viewport.renderedBookCoveragePct,
    coverageLimitPct: bookCoverageLimit(),
    priceScaleMode: viewport.priceScaleMode,
    autoFitBook: viewport.autoFitBook,
    bookOutsideViewport: viewport.bookOutsideViewport,
    lockedBookLogHalfSpan: autoBookViewport.logHalfSpan,
    priceScaleRevision: autoBookViewport.scaleRevision,
    priceScaleRefitPending: autoBookViewport.refitPending,
    priceScaleSourceDomRevision: autoBookViewport.sourceDomRevision,
    priceScaleLastObservedDomRevision:
      autoBookViewport.lastObservedDomRevision,
    rowHeight: viewport.rowHeight,
    followPrice: viewport.follow,
    priceCenterBucket: viewport.centerBucket,
    edgeResetLatched: viewport.edgeResetLatched,
    edgeRecenterCount: viewport.edgeRecenterCount,
    coverageLatchedMarket:
      lastTrustedBookBounds && lastTrustedBookBounds.marketKey
        ? lastTrustedBookBounds.marketKey
        : null,
    axisWidth,
    domWidth,
    chartWidth: rect.width,
    touchGestureMode: touchGesture.mode,
    colWidth: viewport.colWidth,
    rawTimeShiftPx,
    timeShiftPx,
    plotRight,
    liveX: timeline.liveX,
    historyCacheRight,
    historyRight,
    liveBookLeft,
    nowSeamPx: Math.max(0, liveBookLeft - historyRight),
    nowBridgeOverlapPx: Math.max(0, historyRight - liveBookLeft),
    liveBookWidth: timeline.gutterWidth,
    liveBookRatio: timeline.gutterWidth / plotRight,
    latestBubbleX,
    latestBubbleBeforeNow:
      latestBubbleX === null || latestBubbleX <= timeline.liveX,
    liveBookCurrent: liveBookIsCurrent(),
  };
}
window.__lmlDiagnostics = lmlDiagnostics;

// 仅在本机验收参数下把只读快照镜像到隐藏 DOM；正式页面没有额外定时工作。
if (new URLSearchParams(window.location.search).has("verify")) {
  const diagnosticsOutput = document.createElement("output");
  diagnosticsOutput.id = "lmlDiagnostics";
  diagnosticsOutput.hidden = true;
  document.body.appendChild(diagnosticsOutput);
  const updateDiagnosticsOutput = () => {
    const snapshot = JSON.stringify(lmlDiagnostics());
    diagnosticsOutput.textContent = snapshot;
    canvas.dataset.diagnostics = snapshot;
  };
  updateDiagnosticsOutput();
  setInterval(updateDiagnosticsOutput, 1000);
}

// Sandbox-only observer status: persistence is bounded independently of accounts.
async function refreshSandboxStorage() {
  try {
    const response = await fetch(appPath('/api/status'), { cache: 'no-store' });
    if (!response.ok) return;
    const payload = await response.json();
    const storage = payload.storage;
    const badge = el('storageBadge');
    if (!storage || !badge) return;
    const degraded = payload.lastError || storage.lastError || storage.dropped || payload.tradeDrops;
    badge.textContent = degraded
      ? '历史采集异常 · 查看提示'
      : `保留 10 分钟 · ${(storage.bytes / 1048576).toFixed(1)} / ${(storage.maxBytes / 1048576).toFixed(0)} MB`;
    badge.title = `${payload.lastError || storage.lastError || '每秒采样沙盒盘口，逐笔保存沙盒成交'}；容量淘汰 ${storage.capacityEvicted}；丢弃 ${storage.dropped + payload.tradeDrops}`;
    if (degraded || storage.capacityEvicted) badge.style.color = '#fbbf24';
    else badge.style.color = '';
  } catch { /* Reconnect status already covers connection errors. */ }
}
refreshSandboxStorage();
setInterval(refreshSandboxStorage, 10000);
setInterval(loadSymbols, 10000);


window.addEventListener("message", event => {
  if (event.origin !== window.location.origin || event.source !== window.parent || event.data?.type !== "sandbox-maker-live" || !Array.isArray(event.data.symbols)) return;
  enabledMakerSymbols = new Set(event.data.symbols.filter(x => typeof x === "string"));
  populateSymbolList(selectedTarget(), { restore: false });
});

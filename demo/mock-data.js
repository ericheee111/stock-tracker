/* =========================================================================
 * mock-data.js —— 《私享股池》纯前端 Demo 的假数据
 * 覆盖 A股 / 港股 / 美股 三大市场，无任何网络请求。
 * 全局变量 MOCK，供 app.js 直接读取。
 * ========================================================================= */

/* 信号五级定义：
 *   1 强买 / 2 关注买 / 3 观察 / 4 关注卖 / 5 强卖
 * 这里把级别用数字存储，渲染时再映射成文案 + 颜色。
 */
const SIGNAL_LEVELS = {
  1: { label: '强买', color: '#16a34a' },
  2: { label: '关注买', color: '#65a30d' },
  3: { label: '观察', color: '#9ca3af' },
  4: { label: '关注卖', color: '#f97316' },
  5: { label: '强卖', color: '#dc2626' },
};

/* 三大市场指数卡片（带涨跌幅） */
const INDICES = [
  { market: 'A', name: '上证指数', value: 3187.42, changePct: 0.82 },
  { market: 'A', name: '深证成指', value: 10123.55, changePct: 1.24 },
  { market: 'A', name: '创业板指', value: 2034.88, changePct: -0.56 },
  { market: 'HK', name: '恒生指数', value: 17654.21, changePct: 1.05 },
  { market: 'HK', name: '恒生科技', value: 3892.66, changePct: 1.78 },
  { market: 'US', name: '道琼斯', value: 38712.34, changePct: -0.32 },
  { market: 'US', name: '纳斯达克', value: 16342.11, changePct: 0.94 },
  { market: 'US', name: '标普500', value: 5187.22, changePct: 0.21 },
];

/* 个股数据：A股 / 港股 / 美股 各 7 只，字段含义见 PRD
 *   name 名称 / code 代码 / market A|HK|US
 *   price 现价 / changePct 涨跌幅(%) / signalLevel 信号级别
 *   reasons 人话理由清单（ok:true 打勾 ✅，ok:false 叹号 ⚠️）
 *   tags 推荐标签 / industry 行业 / group 自选分组(长线/短线观察)
 */
const STOCKS = [
  // ---------------- A股 ----------------
  { market: 'A', code: '600519', name: '贵州茅台', price: 1685.00, changePct: 1.86, signalLevel: 2,
    industry: '白酒', tags: ['#突破平台', '#低位放量'], group: '长线',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD 红柱放大', ok: true },
      { name: '量能跟上', ok: true }, { name: 'RSI', ok: false, note: '62 中性偏强，未超买' }] },
  { market: 'A', code: '000858', name: '五粮液', price: 142.30, changePct: 0.92, signalLevel: 3,
    industry: '白酒', tags: ['#回踩支撑'], group: '长线',
    reasons: [{ name: '均线纠缠', ok: false, note: '短期均线粘合' }, { name: 'MACD', ok: true },
      { name: '量能', ok: false, note: '缩量' }] },
  { market: 'A', code: '300750', name: '宁德时代', price: 185.66, changePct: 3.42, signalLevel: 1,
    industry: '新能源', tags: ['#刚金叉', '#低位放量'], group: '短线观察',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD 金叉', ok: true },
      { name: '量能放大', ok: true }, { name: 'RSI', ok: false, note: '68 接近超买' }] },
  { market: 'A', code: '601318', name: '中国平安', price: 46.18, changePct: -0.74, signalLevel: 4,
    industry: '保险', tags: ['#超卖反弹'], group: '长线',
    reasons: [{ name: '均线死叉', ok: false }, { name: 'MACD 绿柱', ok: false },
      { name: '量能', ok: true }] },
  { market: 'A', code: '000333', name: '美的集团', price: 68.92, changePct: 1.15, signalLevel: 2,
    industry: '家电', tags: ['#突破平台', '#回踩支撑'], group: '长线',
    reasons: [{ name: '均线多头', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: true }] },
  { market: 'A', code: '002594', name: '比亚迪', price: 241.50, changePct: 2.63, signalLevel: 2,
    industry: '新能源', tags: ['#刚金叉', '#低位放量'], group: '短线观察',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: true }] },
  { market: 'A', code: '600036', name: '招商银行', price: 35.77, changePct: -0.28, signalLevel: 3,
    industry: '银行', tags: ['#回踩支撑'], group: '长线',
    reasons: [{ name: '均线纠缠', ok: false, note: '横盘整理' }, { name: 'MACD', ok: true }] },

  // ---------------- 港股 ----------------
  { market: 'HK', code: '00700', name: '腾讯控股', price: 372.80, changePct: 2.15, signalLevel: 1,
    industry: '互联网', tags: ['#刚金叉', '#突破平台'], group: '长线',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD 红柱', ok: true },
      { name: '量能放大', ok: true }, { name: 'RSI', ok: false, note: '65 未超买' }] },
  { market: 'HK', code: '09988', name: '阿里巴巴-W', price: 78.45, changePct: 1.32, signalLevel: 2,
    industry: '互联网', tags: ['#低位放量'], group: '长线',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: true }] },
  { market: 'HK', code: '03690', name: '美团-W', price: 132.10, changePct: -1.05, signalLevel: 4,
    industry: '本地生活', tags: ['#超卖反弹'], group: '短线观察',
    reasons: [{ name: '均线死叉', ok: false }, { name: 'MACD 绿柱', ok: false }, { name: '量能', ok: true }] },
  { market: 'HK', code: '01810', name: '小米集团-W', price: 19.86, changePct: 4.21, signalLevel: 1,
    industry: '消费电子', tags: ['#刚金叉', '#低位放量', '#突破平台'], group: '短线观察',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD 金叉', ok: true }, { name: '量能放大', ok: true }] },
  { market: 'HK', code: '00939', name: '建设银行', price: 6.12, changePct: 0.33, signalLevel: 3,
    industry: '银行', tags: ['#回踩支撑'], group: '长线',
    reasons: [{ name: '均线纠缠', ok: false }, { name: 'MACD', ok: true }] },
  { market: 'HK', code: '02318', name: '中国平安', price: 41.55, changePct: -0.48, signalLevel: 4,
    industry: '保险', tags: ['#超卖反弹'], group: '长线',
    reasons: [{ name: '均线死叉', ok: false }, { name: 'MACD 绿柱', ok: false }] },
  { market: 'HK', code: '09618', name: '京东集团-SW', price: 108.70, changePct: 1.88, signalLevel: 2,
    industry: '电商', tags: ['#低位放量'], group: '短线观察',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: true }] },

  // ---------------- 美股 ----------------
  { market: 'US', code: 'AAPL', name: '苹果', price: 228.52, changePct: 0.65, signalLevel: 2,
    industry: '消费电子', tags: ['#突破平台'], group: '长线',
    reasons: [{ name: '均线多头', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: false, note: '温和' }] },
  { market: 'US', code: 'TSLA', name: '特斯拉', price: 251.44, changePct: -2.31, signalLevel: 4,
    industry: '新能源车', tags: ['#超卖反弹'], group: '短线观察',
    reasons: [{ name: '均线死叉', ok: false }, { name: 'MACD 绿柱', ok: false }, { name: 'RSI', ok: false, note: '34 接近超卖' }] },
  { market: 'US', code: 'NVDA', name: '英伟达', price: 124.30, changePct: 3.08, signalLevel: 1,
    industry: '半导体', tags: ['#刚金叉', '#低位放量', '#突破平台'], group: '长线',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD 金叉', ok: true }, { name: '量能放大', ok: true }] },
  { market: 'US', code: 'MSFT', name: '微软', price: 442.19, changePct: 0.92, signalLevel: 2,
    industry: '软件', tags: ['#回踩支撑'], group: '长线',
    reasons: [{ name: '均线多头', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: true }] },
  { market: 'US', code: 'AMZN', name: '亚马逊', price: 185.07, changePct: 1.45, signalLevel: 2,
    industry: '电商', tags: ['#低位放量'], group: '长线',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD', ok: true }, { name: '量能', ok: true }] },
  { market: 'US', code: 'AMD', name: 'AMD', price: 162.88, changePct: 2.77, signalLevel: 2,
    industry: '半导体', tags: ['#刚金叉', '#突破平台'], group: '短线观察',
    reasons: [{ name: '均线金叉', ok: true }, { name: 'MACD 金叉', ok: true }, { name: '量能放大', ok: true }] },
  { market: 'US', code: 'META', name: 'Meta', price: 503.76, changePct: -0.88, signalLevel: 3,
    industry: '互联网', tags: ['#回踩支撑'], group: '长线',
    reasons: [{ name: '均线纠结', ok: false }, { name: 'MACD', ok: true }] },
];

/* 今日异动卡片流（放量大涨 / 创新高的票） */
const MOVER = [
  { market: 'A', code: '300750', name: '宁德时代', desc: '放量大涨 +3.42%，刚金叉，量能跟上来了👀', changePct: 3.42 },
  { market: 'HK', code: '01810', name: '小米集团-W', desc: '创近一年新高，三标签共振，资金很活跃', changePct: 4.21 },
  { market: 'US', code: 'NVDA', name: '英伟达', desc: '突破平台 +3.08%，半导体板块集体走强🚀', changePct: 3.08 },
  { market: 'HK', code: '00700', name: '腾讯控股', desc: '低位放量 +2.15%，均线刚金叉，可以盯一下', changePct: 2.15 },
  { market: 'A', code: '002594', name: '比亚迪', desc: '回踩支撑后反包 +2.63%，短线情绪回暖', changePct: 2.63 },
];

/* 个股推荐（小红书式种草卡片流），tags 用于顶部筛选 */
const RECOMMEND = [
  { market: 'A', code: '300750', name: '宁德时代', desc: '新能源龙头刚金叉，量也跟上来了，可以盯一下👀',
    tags: ['#刚金叉', '#低位放量'], changePct: 3.42 },
  { market: 'HK', code: '01810', name: '小米集团-W', desc: '三标签共振 + 创一年新高，这波反弹挺有劲儿💪',
    tags: ['#刚金叉', '#低位放量', '#突破平台'], changePct: 4.21 },
  { market: 'US', code: 'NVDA', name: '英伟达', desc: '突破平台，半导体最强 alpha，回调就是机会',
    tags: ['#刚金叉', '#低位放量', '#突破平台'], changePct: 3.08 },
  { market: 'A', code: '600519', name: '贵州茅台', desc: '回踩支撑后稳住，长线底仓拿得住就别慌',
    tags: ['#突破平台', '#低位放量'], changePct: 1.86 },
  { market: 'HK', code: '00700', name: '腾讯控股', desc: '低位放量的真香票，估值不贵，慢慢建仓',
    tags: ['#刚金叉', '#突破平台'], changePct: 2.15 },
  { market: 'A', code: '002594', name: '比亚迪', desc: '刚金叉 + 低位放量，短线弹性不错',
    tags: ['#刚金叉', '#低位放量'], changePct: 2.63 },
  { market: 'US', code: 'AMD', name: 'AMD', desc: '突破平台，跟着英伟达喝汤的选手🍜',
    tags: ['#刚金叉', '#突破平台'], changePct: 2.77 },
  { market: 'A', code: '000333', name: '美的集团', desc: '均线多头 + 回踩支撑，家电白马很稳',
    tags: ['#突破平台', '#回踩支撑'], changePct: 1.15 },
  { market: 'US', code: 'TSLA', name: '特斯拉', desc: '跌到超卖区了，想抄底的可以挂个条件单观察',
    tags: ['#超卖反弹'], changePct: -2.31 },
];

/* 默认自选股（初始已加入），group 用于分组展示 */
const DEFAULT_WATCHLIST = [
  '300750', '00700', 'NVDA', '600519', '000333',
];

/* 全部可用标签（用于筛选栏） */
const ALL_TAGS = ['#刚金叉', '#低位放量', '#突破平台', '#回踩支撑', '#超卖反弹'];

/* 行业列表（用于市场筛选页） */
const INDUSTRIES = ['白酒', '新能源', '银行', '保险', '家电', '互联网', '本地生活', '消费电子', '电商', '半导体', '软件'];

/* 汇总成一个全局对象，供 app.js 使用 */
const MOCK = {
  SIGNAL_LEVELS, INDICES, STOCKS, MOVER, RECOMMEND,
  DEFAULT_WATCHLIST, ALL_TAGS, INDUSTRIES,
  // 市场中文名
  MARKET_NAME: { A: 'A股', HK: '港股', US: '美股' },
};

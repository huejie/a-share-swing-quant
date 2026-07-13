import type { Snapshot } from './types';
const base={entry:'¥42.80—43.60',version:'MVP-0.9.3',timestamp:'2026-07-06 16:12',kind:'model' as const};
export const demoSnapshot:Snapshot={
 asOf:'2026-07-06 16:12',
 portfolioStatus:'healthy',portfolioReason:'演示组合满足数量、题材和风险约束',
 market:{state:'震荡偏强',score:68,exposure:72,style:'中盘成长 · 制造优先',summary:'广度继续修复，但成交未同步放大。维持七成仓位，不追逐加速题材。',factors:[
  {name:'A股趋势与广度',score:76,note:'全A 61%站上60日线，连续3日改善'},
  {name:'风格结构',score:71,note:'中证1000相对沪深300转强'},
  {name:'成交与流动性',score:57,note:'成交额低于20日均值4.2%'},
  {name:'全球风险',score:63,note:'美元回落，离岸人民币波动收敛'},
  {name:'情绪与拥挤',score:66,note:'涨停扩散，连板高度仍克制'}]},
 holdings:[
  {...base,code:'002050',name:'三花智控',theme:'机器人执行器',action:'持有',weight:20,currentWeight:19.2,price:44.86,change:1.42,thesis:['60日相对强度位于行业前8%','海外订单能见度改善','回踩后量能结构健康'],invalidation:'收盘跌破 40.90，或题材连续两周降至退潮',protectivePrice:41.72,risk:'7月中旬业绩预告窗口',days:34},
  {...base,code:'300308',name:'中际旭创',theme:'光模块',action:'减仓',weight:16,currentWeight:20.4,price:157.30,change:-0.76,thesis:['算力资本开支持续','盈利预期近4周上修','流动性满足千万元容量'],invalidation:'光模块拥挤度升至90且龙头失速',protectivePrice:149.80,risk:'当前题材拥挤度 78/100',days:51},
  {...base,code:'600690',name:'海尔智家',theme:'出海消费',action:'持有',weight:18,currentWeight:17.6,price:29.14,change:0.31,thesis:['现金流质量稳定','人民币波动对冲组合成长暴露','周线平台突破确认'],invalidation:'周收盘跌破 27.20 且相对强度转负',protectivePrice:27.64,risk:'原材料价格反弹',days:22},
  {...base,code:'688012',name:'中微公司',theme:'半导体设备',action:'加仓',weight:18,currentWeight:9.8,price:176.52,change:2.18,thesis:['国产设备订单趋势改善','题材处于扩散期','突破平台后首次缩量回踩'],invalidation:'收盘跌破 166.00，且设备题材广度低于45%',protectivePrice:165.80,risk:'科创板波动较高，分两次完成',days:9}
 ],
 candidates:[
  {...base,code:'601100',name:'恒立液压',theme:'机器人执行器',action:'待买',weight:15,currentWeight:0,price:68.24,change:0.82,thesis:['液压件景气边际改善','候选池综合排名第2','等待平台放量突破'],invalidation:'未突破 70.10 前不追价',protectivePrice:63.90,risk:'与三花智控共享题材风险',days:0},
  {...base,code:'002463',name:'沪电股份',theme:'AI硬件',action:'待买',weight:16,currentWeight:0,price:43.68,change:-0.24,thesis:['高阶PCB需求明确','盈利预期上修','等候回踩确认'],invalidation:'跌破 40.80 或题材进入拥挤期',protectivePrice:40.70,risk:'算力链整体拥挤',days:0},
  {...base,code:'600309',name:'万华化学',theme:'顺周期修复',action:'待买',weight:15,currentWeight:0,price:82.36,change:0.12,thesis:['估值处历史低位','价差边际企稳','用于降低组合成长暴露'],invalidation:'产品价差再创新低',protectivePrice:77.50,risk:'需求修复节奏不确定',days:0}
 ],
 themes:[
  {name:'机器人执行器',phase:'扩散',score:82,breadth:76,flow:74,crowding:61,note:'从龙头向核心零部件扩散'},
  {name:'半导体设备',phase:'启动',score:78,breadth:68,flow:71,crowding:48,note:'订单与价格信号同步改善'},
  {name:'出海消费',phase:'健康趋势',score:73,breadth:64,flow:58,crowding:39,note:'低拥挤的组合平衡项'},
  {name:'光模块',phase:'加速',score:72,breadth:55,flow:84,crowding:78,note:'趋势强，但停止新增仓位'},
  {name:'创新药',phase:'潜伏',score:64,breadth:52,flow:46,crowding:31,note:'催化增加，尚未价格确认'},
  {name:'低空经济',phase:'退潮',score:42,breadth:31,flow:27,crowding:69,note:'龙头与中位数同步走弱'}],
 decisions:[
  {id:'D-0706-01',date:'07-06 16:12',title:'中微公司：目标仓位提高至18%',reason:'半导体设备进入扩散期，回踩结构确认。',version:'MVP-0.9.3',result:'待执行'},
  {id:'D-0706-02',date:'07-06 16:12',title:'中际旭创：降低目标仓位4%',reason:'题材拥挤度升至78，保留核心仓位。',version:'MVP-0.9.3',result:'待执行'},
  {id:'D-0701-01',date:'07-01 16:08',title:'市场仓位上限由64%升至72%',reason:'市场广度连续三日站上阈值。',version:'MVP-0.9.2',result:'已生效'},
  {id:'D-0627-03',date:'06-27 16:10',title:'退出低空经济观察',reason:'题材广度与成交占比同步衰减。',version:'MVP-0.9.2',result:'避免回撤 -4.1%'}],
 equity:[100,101.2,100.7,103.4,105.1,104.2,107.8,109.4,108.9,112.6,111.8,115.2]
};

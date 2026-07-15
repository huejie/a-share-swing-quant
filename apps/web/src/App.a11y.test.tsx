import axe from 'axe-core';
import {readFileSync} from 'node:fs';
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,describe,expect,it,vi} from 'vitest';
import App from './App';

const stylesText=readFileSync('src/styles.css','utf8');

afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()});

const response=(body:unknown,ok=true)=>Promise.resolve({ok,json:async()=>body});

function live(overrides:Record<string,unknown>={}){
 return {as_of:'2026-07-07T16:10:00+08:00',provider:'licensed-provider',
  quality:{freshness:'fresh',status:'healthy'},portfolio_status:'healthy',
  market:{regime:'震荡偏强',score:68,exposure_cap:.72,style:'成长占优',reasons:['市场广度改善'],components:{trend_breadth:76,style:62,liquidity:58,global_risk:64,sentiment:66,valuation:57}},
  portfolio:[0,1,2,3].map(i=>({symbol:`60000${i}.SH`,name:`测试股票${i+1}`,theme:i<2?'先进制造':`题材${i}`,action:'持有',target_weight:.18,current_weight:.17,entry_price:10+i,initial_stop:9,thesis:['趋势健康'],invalidation:'跌破结构',risk_notes:['事件风险'],model_version:'v1',data_timestamp:'2026-07-07'})),
  candidates:[],themes:[],...overrides};
}

function mockApi(body:Record<string,unknown>){
 vi.stubGlobal('fetch',vi.fn().mockImplementation((url:string)=>url.includes('/decisions')?response({items:[]}):response(body)));
}

async function renderRoute(path:string){
 vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('offline')));
 render(<MemoryRouter initialEntries={[path]}><App/></MemoryRouter>);
 await screen.findAllByText('演示数据');
 if(path==='/settings')await waitFor(()=>expect(screen.getByRole('button',{name:'保存设置'})).not.toBeDisabled());
}

describe('整页自动化可访问性',()=>{
 it.each(['/', '/settings', '/risk', '/backtest'])('%s 无 critical/serious axe 违规',async path=>{
  await renderRoute(path);
  // jsdom has no canvas-backed pixel engine; contrast is verified separately
  // in browser/manual QA, while every structural axe rule runs here.
  const result=await axe.run(document.body,{rules:{'color-contrast':{enabled:false}}});
  const severe=result.violations.filter(v=>v.impact==='critical'||v.impact==='serious');
  expect(severe.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}))).toEqual([]);
 });

 it('关键控件可键盘聚焦，表单有标签且触控区域至少44px',async()=>{
  await renderRoute('/settings');
  const capital=screen.getByLabelText('模拟资金');
  const button=screen.getByRole('button',{name:'保存设置'});
  capital.focus();expect(document.activeElement).toBe(capital);
  button.focus();expect(document.activeElement).toBe(button);
  expect(stylesText).toMatch(/button,a,input,select,summary\{[^}]*min-height:44px/);
  const checkbox=screen.getByLabelText('数据过期时停止生成新建议');
  const label=checkbox.closest('label');
  expect(label).not.toBeNull();
  expect(stylesText).toMatch(/\.settings \.check\{[^}]*min-height:44px/);
  expect(stylesText).toContain(':focus-visible');
 });
});

describe('关键数据状态',()=>{
 it('partial 明确说明未凑满且保留现金',async()=>{
  const body=live({portfolio_status:'partial',portfolio_reason:'只有2只股票通过全部门槛',portfolio:(live().portfolio as unknown[]).slice(0,2)});
  mockApi(body);render(<MemoryRouter><App/></MemoryRouter>);
  await screen.findByText('组合未凑满');
  expect(screen.getByText(/只有2只股票通过全部门槛/)).toBeInTheDocument();
  expect(screen.getByRole('heading',{level:1,name:/模型组合 2 只/})).toBeInTheDocument();
 });

 it('observation 保留组合展示但明确不是生产建议',async()=>{
  mockApi(live({portfolio_status:'observation',portfolio_reason:'公开来源前瞻记录'}));
  render(<MemoryRouter><App/></MemoryRouter>);
  expect(await screen.findByText('前瞻模拟观察，不是生产建议')).toBeInTheDocument();
  expect(screen.getByText('公开来源前瞻记录')).toBeInTheDocument();
  expect(screen.getByText('测试股票1')).toBeInTheDocument();
 });

 it('risk_off 显示保持现金而非补股',async()=>{
  mockApi(live({portfolio_status:'risk_off',portfolio_reason:'极端风险状态暂停新建议',portfolio:[],market:{regime:'极端风险',score:20,exposure_cap:0,style:'防御',reasons:['风险快速上升'],components:{trend_breadth:20}}}));
  render(<MemoryRouter><App/></MemoryRouter>);
  expect(await screen.findByText('风险关闭：保持现金')).toBeInTheDocument();
  expect(screen.getByText('极端风险状态暂停新建议')).toBeInTheDocument();
 });

 it('stale 持续告警并明确暂停新建议',async()=>{
  mockApi(live({quality:{freshness:'stale',status:'blocked'}}));
  render(<MemoryRouter><App/></MemoryRouter>);
  await waitFor(()=>expect(screen.getAllByText('数据已过期').length).toBeGreaterThan(0));
  expect(screen.getByText(/新建议已暂停/)).toBeInTheDocument();
 });

 it('provider failure 明确降级为演示数据',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('provider unavailable')));
  render(<MemoryRouter><App/></MemoryRouter>);
  expect((await screen.findAllByText('演示数据')).length).toBeGreaterThan(0);
  expect(screen.getByText(/实时接口暂不可用/)).toBeInTheDocument();
 });
});

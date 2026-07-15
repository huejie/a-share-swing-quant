import{afterEach,describe,expect,it,vi}from'vitest';
import{getSettings,runBacktest,saveSettings}from'./api';

afterEach(()=>{sessionStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()});

describe('管理请求边界',()=>{
 it.each([
  ['回测',()=>runBacktest()],
  ['设置',()=>saveSettings({capital:1000000})]
 ])('%s请求只从sessionStorage发送管理密钥',async(_label,request)=>{
  sessionStorage.setItem('quant_admin_api_key','session-secret');
  const fetchMock=vi.fn().mockResolvedValue({ok:true,json:async()=>({})});
  vi.stubGlobal('fetch',fetchMock);
  await request();
  const options=fetchMock.mock.calls[0][1];
  expect(options.headers['X-Admin-Key']).toBe('session-secret');
  expect(localStorage.getItem('quant_admin_api_key')).toBeNull();
 });

 it('只读设置请求不携带管理密钥',async()=>{
  sessionStorage.setItem('quant_admin_api_key','session-secret');
  const fetchMock=vi.fn().mockResolvedValue({ok:true,json:async()=>({capital:3000000})});
  vi.stubGlobal('fetch',fetchMock);
  await getSettings();
  expect(fetchMock.mock.calls[0][1].headers).toEqual({Accept:'application/json'});
 });
});

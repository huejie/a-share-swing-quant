import {createReadStream, promises as fs} from 'node:fs';
import http from 'node:http';
import path from 'node:path';

const port=Number(process.env.PORT||8080);
const publicDir='/app/dist';
const mime={'.css':'text/css; charset=utf-8','.html':'text/html; charset=utf-8','.ico':'image/x-icon',
  '.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8','.png':'image/png',
  '.svg':'image/svg+xml','.webp':'image/webp'};

function headers(res){
  res.setHeader('X-Content-Type-Options','nosniff');
  res.setHeader('X-Frame-Options','SAMEORIGIN');
  res.setHeader('Referrer-Policy','strict-origin-when-cross-origin');
}

function proxy(req,res){
  const upstream=http.request({hostname:'api',port:8000,path:req.url,method:req.method,
    headers:{...req.headers,host:'api:8000'}},response=>{
      headers(res);res.writeHead(response.statusCode||502,response.headers);response.pipe(res);
    });
  upstream.setTimeout(360_000,()=>upstream.destroy(new Error('upstream timeout')));
  upstream.on('error',()=>{if(!res.headersSent){headers(res);res.writeHead(502,{'Content-Type':'application/json; charset=utf-8'});}
    res.end(JSON.stringify({error:{code:'UPSTREAM_UNAVAILABLE',message:'API 服务暂不可用'}}));});
  req.pipe(upstream);
}

async function staticFile(req,res){
  let pathname;
  try{pathname=decodeURIComponent(new URL(req.url||'/','http://localhost').pathname);}catch{res.writeHead(400);res.end();return;}
  let filename=path.resolve(publicDir,`.${pathname}`);
  if(filename!==publicDir&&!filename.startsWith(`${publicDir}${path.sep}`)){res.writeHead(403);res.end();return;}
  try{if((await fs.stat(filename)).isDirectory())filename=path.join(filename,'index.html');}
  catch{filename=path.join(publicDir,'index.html');}
  try{
    const stat=await fs.stat(filename);headers(res);res.writeHead(200,{'Content-Type':mime[path.extname(filename)]||'application/octet-stream',
      'Content-Length':stat.size,'Cache-Control':filename.endsWith('index.html')?'no-cache':'public, max-age=31536000, immutable'});
    if(req.method==='HEAD')res.end();else createReadStream(filename).pipe(res);
  }catch{headers(res);res.writeHead(404);res.end('Not found');}
}

http.createServer((req,res)=>{
  if((req.url||'').startsWith('/api/')||(req.url||'')==='/health')proxy(req,res);
  else if(req.method==='GET'||req.method==='HEAD')void staticFile(req,res);
  else{headers(res);res.writeHead(405,{'Allow':'GET, HEAD'});res.end();}
}).listen(port,'0.0.0.0',()=>console.log(`web gateway listening on ${port}`));

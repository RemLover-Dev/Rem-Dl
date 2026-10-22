// Measure typed-array allocations, not total browser/process memory.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const {performance}=require('perf_hooks');
const source=fs.readFileSync(path.join(__dirname,'../web/script.js'),'utf8');
const baseline=fs.readFileSync(path.join(__dirname,'../tests/fixtures/lanczos-before.cjs'),'utf8');
const sw=2840,sh=4125,dw=690,dh=1002;
const pixels=Uint8ClampedArray.from({length:sw*sh*4},(_,i)=>(i*71+17)%256);
function run(code) {
 let floatBytes=0,maxFloatBytes=0;
 class CountedFloat32 extends Float32Array {constructor(n){super(n);floatBytes+=this.byteLength;maxFloatBytes=Math.max(maxFloatBytes,this.byteLength);}}
 const ctx={Float32Array:CountedFloat32,Uint8ClampedArray,Math,Map,Array,module:{exports:null}};vm.createContext(ctx);
 vm.runInContext(code,ctx);
 const start=performance.now();const output=ctx.viewerLanczosResize(pixels,sw,sh,dw,dh);
 return {floatBytes,maxFloatBytes,ms:performance.now()-start,output};
}
const old=run(baseline),updated=run(source.slice(source.indexOf('function viewerLanczosResize('),source.indexOf('let viewerFitRaster =')));
assert.deepEqual(updated.output,old.output,'Filter output changed');
for(const [name,r] of [['Before',old],['Updated',updated]])console.log(`${name}: Float32 allocations ${(r.floatBytes/1048576).toFixed(3)} MiB; largest ${(r.maxFloatBytes/1048576).toFixed(3)} MiB; time ${r.ms.toFixed(1)} ms`);
console.log(`Exact output match. Float32 allocation reduction ${(100*(1-updated.floatBytes/old.floatBytes)).toFixed(2)}%. Input/output/decoded/canvas memory is additional.`);

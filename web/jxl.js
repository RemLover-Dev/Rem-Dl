/* JPEG XL settings tools. No uploads, automatic conversion or idle polling. */
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const active = job => ['scanning', 'running', 'cancelling'].includes(job.state);
    let requestEpoch = 0, starting = false;
    let polling = null, fetching = false, busy = false, job = {state: 'idle'}, resultSignature = '';
    async function api(url, body) {
        const response = await fetch(url, body === undefined ? {cache:'no-store'} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Request failed. Refresh status to retry.');
        return data;
    }
    function modeChanged() {
        const mode = $('jxlMode').value;
        $('jxlQuality').disabled = mode !== 'lossy' || busy;
        $('jxlEffort').disabled = mode === 'restore' || busy;
        $('jxlStart').textContent = mode === 'restore' ? 'Restore previous format' : 'Convert to JXL';
        $('jxlModeHint').textContent = {
            auto:'Still PNG, JPG/JPEG and WebP only. JPEGs use reversible recompression; 8-bit PNG/WebP use lossless normalized pixels. 16-bit PNG keeps its original precision and colour profile (requires FFmpeg with libjxl). Rems tag categories stay embedded. Higher effort takes longer.',
            lossless:'Keeps normalized 8-bit pixels exactly. 16-bit PNG retains its original precision and colour profile; every channel is verified. Rems tag categories are kept; other private metadata is removed.',
            lossy:'Quality 1–99: lower values allow more changes. Higher effort takes longer. Rems tag categories are kept; other private metadata is removed.',
            restore:'Reads the previous format from the embedded Rems conversion record. Works after restart or moving the JXL. Replaces the JXL with the restored image in the same folder.'
        }[mode];
    }
    function controls() {
        busy = starting || active(job);
        for (const id of ['jxlStart','jxlPath','jxlMode','jxlBrowseFile','jxlBrowseFolder','jxlRecursive']) $(id).disabled = busy;
        $('jxlCancel').disabled = !active(job) || job.state === 'cancelling';
        $('jxlReveal').disabled = !job.output || !(job.completed > 0);
        modeChanged();
    }
    function size(value) { return (value / (1024 * 1024)).toFixed(2) + ' MiB'; }
    function render(next) {
        job = next;
        controls();
        const done = (job.completed || 0) + (job.failed || 0);
        $('jxlProgress').max = Math.max(job.total || 1, 1);
        $('jxlProgress').value = done;
        let message = job.state === 'idle' ? 'Ready. Verified results replace source images in the same folder.' : `${job.state}: ${done}/${job.total || 0} processed · ${job.completed || 0} completed · ${job.failed || 0} failed`;
        if (job.parallel_workers) message += ` · ${(job.active || []).length} active / ${job.parallel_workers} slots · ${job.cpu_threads} CPU threads shared`;
        if (job.current) message += ' · ' + job.current;
        if (job.error) message += ' · ' + job.error;
        if (job.input_bytes) message += ` · successful files: ${size(job.input_bytes)} → ${size(job.output_bytes)} (sources replaced)`;
        $('jxlStatus').textContent = message;
        $('jxlOutput').textContent = job.output ? 'Location: ' + job.output : '';
        const signature = `${job.id}:${done}`;
        if (signature !== resultSignature) {
            resultSignature = signature;
            if (job.completed > 0 && typeof window.invalidateGalleryBuffer === 'function') window.invalidateGalleryBuffer();
            $('jxlResults').replaceChildren();
            for (const row of (job.results || []).slice().reverse()) {
                const item = document.createElement('div'); item.className = 'jxl-result';
                const text = document.createElement('span');
                text.textContent = row.error ? `${row.source}\nFailed: ${row.error} · original kept` : `${row.output}\n${size(row.input_bytes)} → ${size(row.output_bytes)} · ${row.tag_lines ? 'embedded tags and categories verified' : 'no embedded Rems tags in source'}${row.metrics && row.metrics.exact ? ' · lossless verified' : ''}`;
                item.append(text);
                if (!row.error && row.comparison_available) {
                    const button = document.createElement('button'); button.className = 'action-btn'; button.type = 'button'; button.textContent = 'Compare';
                    button.onclick = () => comparison.open(row.token, button);
                    item.append(button);
                }
                $('jxlResults').append(item);
            }
            if (done > 100) { const note = document.createElement('p'); note.textContent = 'Showing the last 100 results. Completed images are in their original folders. Compare is available for the latest replacement.'; $('jxlResults').append(note); }
        }
    }
    function schedule() {
        clearTimeout(polling); polling = null;
        if (active(job)) polling = setTimeout(refresh, 1000);
    }
    async function refresh() {
        if (fetching) return;
        clearTimeout(polling); polling = null; fetching = true;
        const epoch = requestEpoch;
        try {
            const data = await api('/api/jxl/status');
            if (epoch !== requestEpoch) return;
            render(data.job);
            if (!data.codec_available && !active(job)) {
                $('jxlStart').disabled = true;
                $('jxlStatus').textContent = 'Install the JPEG XL add-on in the Python environment running Rems-Dl: python -m pip install -r requirements-jxl.txt, then restart.';
            }
            schedule();
        } catch (error) { if (epoch === requestEpoch) $('jxlStatus').textContent = error.message; }
        finally { fetching = false; }
    }
    async function browse(kind) {
        const button = $(kind === 'file' ? 'jxlBrowseFile' : 'jxlBrowseFolder'); button.disabled = true;
        try { const result = await api('/api/jxl/browse', {kind}); if (result.path) $('jxlPath').value = result.path; }
        catch (error) { $('jxlStatus').textContent = error.message; }
        finally { controls(); }
    }
    $('jxlBrowseFile').onclick = () => browse('file');
    $('jxlBrowseFolder').onclick = () => browse('folder');
    $('jxlMode').onchange = modeChanged;
    $('jxlRefresh').onclick = refresh;
    $('jxlStart').onclick = async () => {
        if (busy) return;
        requestEpoch++; starting = true; controls();
        comparison.close();
        try {
            const data = await api('/api/jxl/start', {path:$('jxlPath').value, mode:$('jxlMode').value, quality:Number($('jxlQuality').value), effort:Number($('jxlEffort').value), recursive:$('jxlRecursive').checked});
            render(data.job); schedule();
        } catch (error) { $('jxlStatus').textContent = error.message; }
        finally { starting = false; controls(); }
    };
    $('jxlCancel').onclick = async () => {
        requestEpoch++;
        $('jxlCancel').disabled = true;
        try { const data = await api('/api/jxl/cancel', {}); render(data.job); schedule(); }
        catch (error) { $('jxlStatus').textContent = error.message; controls(); }
    };
    $('jxlReveal').onclick = async () => {try {await api('/api/jxl/reveal', {});} catch (error) {$('jxlStatus').textContent = error.message;}};
    // Check once on entering Settings; only an active job schedules updates.
    const options = $('Options');
    new MutationObserver(() => {if (options.style.display !== 'none') refresh();}).observe(options, {attributes:true, attributeFilter:['style']});
    if (options.style.display !== 'none') refresh();

    const comparison = (function () {
        const modal = $('jxlCompareModal'), canvas = $('jxlCompareCanvas'), context = canvas.getContext('2d');
        let blendCanvas = null;
        let images = [], diff = null, diffLoading = false, epoch = 0, token = '', zoom = 1, center = [0,0], amount = .5, fitting = true, drag = null, held = null, frame = null, previousFocus = null;
        const mode = () => $('jxlCompareMode').value;
        const dims = () => [canvas.clientWidth || 1, canvas.clientHeight || 1];
        const clamp = () => {
            if (!images.length) return;
            dims().forEach((v, i) => {const extent = i ? images[0].height : images[0].width; const half = v / (2 * zoom); center[i] = extent <= 2 * half ? extent / 2 : Math.min(extent - half, Math.max(half, center[i]));});
        };
        const requestDraw = () => {if (frame === null && !modal.hidden) frame = requestAnimationFrame(draw);};
        function drawImage(image) {
            const [w,h] = dims();
            let x = w / 2 - center[0] * zoom, y = h / 2 - center[1] * zoom;
            if (zoom >= 1) {x = Math.round(x); y = Math.round(y);}
            context.drawImage(image, x, y, image.width * zoom, image.height * zoom);
        }
        function draw() {
            frame = null;
            if (modal.hidden || images.length !== 2 || !context) return;
            const [w,h] = dims(), ratio = Math.min(window.devicePixelRatio || 1, 2);
            if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);}
            context.setTransform(ratio, 0, 0, ratio, 0, 0); context.globalAlpha = 1;
            context.imageSmoothingEnabled = zoom < 1;
            context.fillStyle = '#d6d9de'; context.fillRect(0,0,w,h); context.fillStyle = '#f5f6f8';
            for (let y=0;y<h;y+=16) for (let x=0;x<w;x+=16) if ((x/16+y/16)%2) context.fillRect(x,y,16,16);
            if (mode() === 'Difference') {
                context.fillStyle = 'black'; context.fillRect(0,0,w,h);
                if (diff) {
                    drawImage(diff);
                    const gain = Number($('jxlDiffGain').value);
                    if (gain > 1) {
                        context.save(); context.setTransform(1,0,0,1,0,0);
                        const pixels = context.getImageData(0,0,canvas.width,canvas.height);
                        for (let i=0;i<pixels.data.length;i+=4) for (let c=0;c<3;c++) pixels.data[i+c] = Math.min(255, pixels.data[i+c]*gain);
                        context.putImageData(pixels,0,0); context.restore();
                    }
                }
            } else if (mode() === 'Original') drawImage(images[0]);
            else if (mode() === 'Converted') drawImage(images[1]);
            else if (mode() === 'Reveal') {
                context.save(); context.beginPath(); context.rect(0,0,w*amount,h); context.clip(); drawImage(images[0]); context.restore();
                context.save(); context.beginPath(); context.rect(w*amount,0,w*(1-amount),h); context.clip(); drawImage(images[1]); context.restore();
                context.strokeStyle='white'; context.lineWidth=2; context.beginPath(); context.moveTo(w*amount,0); context.lineTo(w*amount,h); context.stroke();
                context.beginPath(); context.arc(w*amount,h/2,17,0,Math.PI*2); context.fillStyle='#3859d5'; context.fill(); context.stroke(); context.fillStyle='white'; context.font='16px sans-serif'; context.textAlign='center'; context.fillText('↔',w*amount,h/2+5);
            } else {
                // Blend two independently composited checkerboard views, so alpha
                // is not accidentally applied twice where images overlap.
                const offscreen = blendCanvas || (blendCanvas = document.createElement('canvas')); if(offscreen.width!==canvas.width)offscreen.width=canvas.width;if(offscreen.height!==canvas.height)offscreen.height=canvas.height;
                const other=offscreen.getContext('2d'); other.setTransform(1,0,0,1,0,0);other.globalAlpha=1;other.clearRect(0,0,offscreen.width,offscreen.height);other.drawImage(canvas,0,0);
                other.setTransform(ratio,0,0,ratio,0,0); other.imageSmoothingEnabled=zoom<1;
                let x=w/2-center[0]*zoom,y=h/2-center[1]*zoom;if(zoom>=1){x=Math.round(x);y=Math.round(y);}
                other.drawImage(images[1],x,y,images[1].width*zoom,images[1].height*zoom);
                drawImage(images[0]); context.globalAlpha=amount;context.drawImage(offscreen,0,0,w,h);context.globalAlpha=1;
            }
            $('jxlZoomLabel').textContent=(zoom*100).toFixed(1)+'%';
        }
        function fit() {if(!images.length)return;const [w,h]=dims();zoom=Math.min(w/images[0].width,h/images[0].height,1);center=[images[0].width/2,images[0].height/2];fitting=true;requestDraw();}
        function setZoom(value, cursor=dims().map(v=>v/2)) {
            if (!images.length) return;
            const viewport=dims(), minimum=Math.min(viewport[0]/images[0].width,viewport[1]/images[0].height,1)/4;
            const next=Math.max(minimum,Math.min(16,value));
            center=center.map((c,i)=>c+(cursor[i]-viewport[i]/2)/zoom-(cursor[i]-viewport[i]/2)/next);
            zoom=next;fitting=false;clamp();requestDraw();
        }
        function load(url) {return new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(new Error('Unable to load comparison. Available memory may be insufficient, or the files may have changed.'));img.src=url;});}
        async function modeChanged() {
            $('jxlMixLabel').textContent=mode()==='Fade'?'Converted opacity':'Original reveal';
            $('jxlMix').disabled=!['Reveal','Fade'].includes(mode());$('jxlDiffGain').disabled=mode()!=='Difference';
            requestDraw();
            if(mode()==='Difference'&&!diff&&!diffLoading&&images.length===2){
                const current=epoch;diffLoading=true;$('jxlCompareStatus').textContent='Computing RGB and alpha differences…';
                try{const image=await load(`/api/jxl/compare/${token}/difference`);if(current!==epoch)return;diff=image;$('jxlCompareStatus').textContent='Black = identical display samples. Full 16-bit samples are verified separately during lossless conversion. Gain amplifies differences and can clip bright values.';requestDraw();}
                catch(error){if(current===epoch)$('jxlCompareStatus').textContent=error.message;}
                finally{if(current===epoch)diffLoading=false;}
            }
        }
        function close(){epoch++;modal.hidden=true;images=[];diff=null;diffLoading=false;drag=null;held=null;if(frame!==null)cancelAnimationFrame(frame);frame=null;canvas.width=1;canvas.height=1;if(blendCanvas){blendCanvas.width=1;blendCanvas.height=1;blendCanvas=null;}if(previousFocus&&previousFocus.isConnected)previousFocus.focus();}
        async function open(value,focus){
            close();previousFocus=focus;modal.hidden=false;token=value;amount=.5;$('jxlMix').value=50;$('jxlCompareMode').value='Reveal';modeChanged();$('jxlCompareStatus').textContent='Loading aligned images…';$('jxlCompareClose').focus();const current=epoch;
            try{const pair=await Promise.all([load(`/api/jxl/compare/${token}/original`),load(`/api/jxl/compare/${token}/converted`)]);if(current!==epoch)return;if(pair[0].width!==pair[1].width||pair[0].height!==pair[1].height)throw new Error('Normalized dimensions differ; comparison stopped.');images=pair;fit();canvas.focus();$('jxlCompareStatus').textContent='Aligned sRGB pixels · temporary original snapshot · category metadata verified during replacement.';}
            catch(error){if(current===epoch)$('jxlCompareStatus').textContent=error.message;}
        }
        canvas.addEventListener('pointerdown',e=>{if(images.length!==2)return;canvas.focus();const r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;drag={kind:mode()==='Reveal'&&Math.abs(x-amount*dims()[0])<=24?'divider':'pan',x,y,center:center.slice()};canvas.setPointerCapture(e.pointerId);});
        canvas.addEventListener('pointermove',e=>{const r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;if(!drag){canvas.style.cursor=mode()==='Reveal'&&Math.abs(x-amount*dims()[0])<=24?'ew-resize':'grab';return;}if(drag.kind==='divider'){amount=Math.max(0,Math.min(1,x/dims()[0]));$('jxlMix').value=amount*100;}else{center=[drag.center[0]-(x-drag.x)/zoom,drag.center[1]-(y-drag.y)/zoom];fitting=false;clamp();}requestDraw();});
        for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,()=>{drag=null;});
        canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect();setZoom(zoom*(e.deltaY<0?1.2:1/1.2),[e.clientX-r.left,e.clientY-r.top]);},{passive:false});
        canvas.addEventListener('dblclick',()=>zoom===1?fit():setZoom(1));
        $('jxlMix').oninput=()=>{amount=Number($('jxlMix').value)/100;requestDraw();};
        $('jxlCompareMode').onchange=modeChanged;$('jxlDiffGain').onchange=requestDraw;
        $('jxlFit').onclick=fit;$('jxlActual').onclick=()=>setZoom(1);$('jxlZoomIn').onclick=()=>setZoom(zoom*1.25);$('jxlZoomOut').onclick=()=>setZoom(zoom/1.25);$('jxlCompareClose').onclick=close;
        function release(){if(held!==null){$('jxlCompareMode').value=held;held=null;modeChanged();}}
        window.addEventListener('blur',release);
        document.addEventListener('keydown',e=>{
            if(modal.hidden)return;
            if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();return;}
            if(e.key==='Tab'){
                const elements=[...modal.querySelectorAll('button,select,input,canvas')].filter(n=>!n.disabled);const index=elements.indexOf(document.activeElement);if(e.shiftKey&&index<=0){elements.at(-1).focus();e.preventDefault();}else if(!e.shiftKey&&index===elements.length-1){elements[0].focus();e.preventDefault();}
            }
            if(e.target===canvas){const key=e.key.toLowerCase();if(key===' '){if(held===null){held=mode();$('jxlCompareMode').value='Original';modeChanged();}}else if(key==='f')fit();else if(key==='1')setZoom(1);else if(key==='+'||key==='=')setZoom(zoom*1.25);else if(key==='-')setZoom(zoom/1.25);else return;e.preventDefault();}
            e.stopImmediatePropagation();
        },true);
        document.addEventListener('keyup',e=>{if(!modal.hidden&&e.key===' '){release();e.preventDefault();e.stopImmediatePropagation();}},true);
        const resized=()=>{if(!modal.hidden){if(fitting)fit();else{clamp();requestDraw();}}};
        if(window.ResizeObserver)new ResizeObserver(resized).observe(canvas.parentElement);else window.addEventListener('resize',resized);
        return {open,close};
    })();
})();

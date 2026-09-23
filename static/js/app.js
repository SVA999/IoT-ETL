/* =============================================================================
   NEON AIR - logica de la PWA.
   Sin dependencias externas: la app tiene que arrancar offline desde el cache
   del service worker, y cada libreria seria un punto mas de fallo.
   ========================================================================== */
'use strict';

const $ = (id) => document.getElementById(id);
const state = {
    scale: [],
    stations: [],
    selected: null,
    history: null,
    source: null,
    lastScan: null,
};

/* ------------------------------------------------------------------ helpers */
async function api(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch { /* respuesta no JSON */ }
    if (!response.ok) {
        throw new Error((body && body.error) || `HTTP ${response.status}`);
    }
    return body;
}

function log(message, kind = '') {
    const box = $('console');
    const stamp = new Date().toLocaleTimeString('es-CO', { hour12: false });
    const line = document.createElement('div');
    line.innerHTML = `<span class="hl">[${stamp}]</span> <span class="${kind}"></span>`;
    line.lastElementChild.textContent = message;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
}

const num = (v, digits = 1) =>
    (v === null || v === undefined || Number.isNaN(v)) ? '--' : Number(v).toFixed(digits);

/* ------------------------------------------------------------------ cabecera */
function setSourceChip(dataset) {
    const chip = $('chipSource');
    const text = $('chipText');
    chip.classList.remove('live', 'fallback', 'offline');
    if (!dataset) {
        chip.classList.add('offline');
        text.textContent = 'pipeline calentando';
        return;
    }
    state.source = dataset;
    if (dataset.realtime) {
        chip.classList.add('live');
        text.textContent = `live · ${dataset.stations} estaciones`;
    } else {
        chip.classList.add('fallback');
        text.textContent = `archivo historico · ${dataset.stations} estaciones`;
    }
}

function updateNetChip() {
    const chip = $('chipNet');
    const online = navigator.onLine;
    chip.style.color = online ? 'var(--green)' : 'var(--red)';
    $('netText').textContent = online ? 'online' : 'sin red · cache';
}

function tickClock() {
    $('scanClock').textContent = new Date().toLocaleTimeString('es-CO', { hour12: false });
}

/* ------------------------------------------------------------------ escala ICA */
function renderScale(active) {
    const box = $('scaleBox');
    box.innerHTML = '';
    state.scale.forEach((level) => {
        const row = document.createElement('div');
        row.className = 'row' + (active === level.label ? ' active' : '');
        row.innerHTML = `
            <span class="sw" style="background:${level.color};color:${level.color}"></span>
            <span><b>${level.label}</b></span>
            <span>${level.pm25_min}–${level.pm25_max} µg/m³</span>`;
        box.appendChild(row);
    });
}

/* ------------------------------------------------------------------ escaneo GPS */
function getPosition() {
    return new Promise((resolve, reject) => {
        if (!navigator.geolocation) {
            reject(new Error('Este dispositivo no expone GPS al navegador.'));
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => resolve(pos),
            (err) => {
                const motivos = {
                    1: 'Permiso de ubicacion denegado. Habilitalo en los ajustes del navegador.',
                    2: 'Posicion no disponible en este momento.',
                    3: 'El GPS tardo demasiado en responder.',
                };
                reject(new Error(motivos[err.code] || err.message));
            },
            { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
        );
    });
}

async function queryPoint(lat, lon, accuracy) {
    const data = await api(`/api/air-quality/nearest?lat=${lat}&lon=${lon}`);
    state.lastScan = data;
    renderVerdict(data, accuracy);
    setSourceChip({ realtime: data.source.realtime, stations: state.stations.length });
    log(`Semaforo: ${data.traffic_light.label} · PM2.5 ${num(data.estimate.pm25)} µg/m³`,
        data.traffic_light.good ? 'ok' : 'warn');
    await selectStation(data.nearest_station.station_id);
}

async function scan() {
    const button = $('btnScan');
    const label = $('btnScanText');
    button.disabled = true;
    button.classList.add('working');
    label.textContent = '◌ LEYENDO GPS...';
    try {
        if (!window.isSecureContext) {
            log('Contexto no seguro: los navegadores solo entregan GPS por https o localhost.', 'warn');
        }
        const position = await getPosition();
        const { latitude, longitude, accuracy } = position.coords;
        log(`GPS ${latitude.toFixed(5)}, ${longitude.toFixed(5)} (±${Math.round(accuracy)} m)`, 'hl');
        label.textContent = '◌ CONSULTANDO SIATA...';
        $('manualLat').value = latitude.toFixed(5);
        $('manualLon').value = longitude.toFixed(5);
        await queryPoint(latitude, longitude, accuracy);
    } catch (error) {
        log(`Escaneo fallido: ${error.message}`, 'err');
        renderVerdictError(`${error.message} Puedes consultar un punto manualmente aqui abajo.`);
        $('manualBox').classList.remove('hidden');
    } finally {
        button.disabled = false;
        button.classList.remove('working');
        label.textContent = '◉ ESCANEAR MI ZONA';
    }
}

async function scanManual() {
    const lat = parseFloat($('manualLat').value);
    const lon = parseFloat($('manualLon').value);
    if (Number.isNaN(lat) || Number.isNaN(lon)) {
        log('Coordenadas invalidas.', 'err');
        return;
    }
    const button = $('btnManual');
    button.disabled = true;
    try {
        log(`Consulta manual ${lat}, ${lon}`, 'hl');
        await queryPoint(lat, lon, null);
    } catch (error) {
        log(`Consulta fallida: ${error.message}`, 'err');
        renderVerdictError(error.message);
    } finally {
        button.disabled = false;
    }
}

function renderVerdict(data, accuracy) {
    const light = data.traffic_light;
    const box = $('verdict');
    box.classList.remove('sindato');
    box.style.setProperty('--status', light.color);
    $('vValue').textContent = light.value === null ? '--' : num(light.value);
    $('vIca').textContent = light.ica ?? '--';
    $('vLabel').textContent = light.good === null
        ? 'SIN DATO'
        : (light.good ? `AIRE OK · ${light.label}` : `CUIDADO · ${light.label}`);
    $('vAdvice').textContent = light.advice;
    $('vGauge').style.width = `${Math.min(100, ((light.ica ?? 0) / 300) * 100)}%`;

    const station = data.nearest_station;
    const contributors = (data.estimate.contributors || [])
        .map((c) => `${c.station_code} ${Math.round((c.weight || 0) * 100)}%`)
        .join(' · ');
    $('vMeta').innerHTML = `
        Estacion mas cercana: <b>${station.station_name}</b> (${station.station_code}),
        a <b>${num(station.distance_km, 2)} km</b>.<br>
        Base de calculo: <b>${data.estimate.basis}</b> ·
        interpolacion espacial <b>${data.estimate.spatial_method}</b>${contributors ? ` → ${contributors}` : ''}.<br>
        Dato de la estacion: <b>${station.is_imputed ? 'imputado por el ensamble' : 'observado'}</b>,
        confianza <b>${num(station.confidence * 100, 0)}%</b>, sello <b>${station.timestamp.replace('T', ' ')}</b>.<br>
        Fuente: <b>${data.source.realtime ? 'SIATA en vivo' : 'archivo historico SIATA'}</b>
        ${accuracy ? `· precision GPS ±${Math.round(accuracy)} m` : ''}`;
    renderScale(light.label);
}

function renderVerdictError(message) {
    const box = $('verdict');
    box.classList.add('sindato');
    box.style.removeProperty('--status');
    $('vValue').textContent = '--';
    $('vIca').textContent = '--';
    $('vLabel').textContent = 'ESCANEO FALLIDO';
    $('vAdvice').textContent = message;
    $('vMeta').textContent = '';
    $('vGauge').style.width = '0%';
}

/* ------------------------------------------------------------------ estaciones */
async function loadStations() {
    const data = await api('/api/stations');
    state.stations = data.stations;
    setSourceChip({ realtime: data.realtime, stations: data.count });
    $('stationCount').textContent = `${data.count} activas · ${data.source}`;

    const box = $('stations');
    box.innerHTML = '';
    data.stations
        .slice()
        .sort((a, b) => (b.pm25_24h ?? 0) - (a.pm25_24h ?? 0))
        .forEach((s) => {
            const value = s.pm25_24h ?? s.pm25_hour;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'station';
            button.style.setProperty('--dot', s.traffic_light.color);
            button.dataset.id = s.station_id;
            button.innerHTML = `
                <span class="led"></span>
                <span>
                    <span class="name"></span>
                    <span class="sub">${s.station_code} · ${s.data_quality.availability_final_pct}% datos${s.is_imputed ? ' · imputado' : ''}</span>
                </span>
                <span class="val">${num(value)}<small>µg/m³</small></span>`;
            button.querySelector('.name').textContent = s.station_name;
            button.addEventListener('click', () => selectStation(s.station_id));
            box.appendChild(button);
        });
    return data.stations;
}

async function selectStation(stationId) {
    state.selected = stationId;
    document.querySelectorAll('.station').forEach((el) => {
        el.setAttribute('aria-current', String(Number(el.dataset.id) === Number(stationId)));
    });
    const station = state.stations.find((s) => s.station_id === Number(stationId));
    $('chartStation').textContent = station ? `${station.station_code} · ${station.station_name}` : '';
    try {
        state.history = await api(`/api/stations/${stationId}/history?limit=120`);
        drawChart(state.history.points);
    } catch (error) {
        log(`No se pudo cargar la serie: ${error.message}`, 'err');
    }
}

/* ------------------------------------------------------------------ grafica */
function drawChart(points) {
    const canvas = $('chart');
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 900;
    const height = Math.max(240, Math.min(380, Math.round(width * 0.45)));
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!points || !points.length) return;

    const pad = { l: 42, r: 12, t: 14, b: 26 };
    const plotW = width - pad.l - pad.r;
    const plotH = height - pad.t - pad.b;

    const values = points.flatMap((p) => [p.final, p.clean, p.ensemble, p.linear, p.cubic, p.nearest])
        .filter((v) => v !== null && v !== undefined);
    const maxValue = Math.max(12, ...values) * 1.12;
    const x = (i) => pad.l + (i / Math.max(1, points.length - 1)) * plotW;
    const y = (v) => pad.t + plotH - (v / maxValue) * plotH;

    // rejilla + bandas de la escala ICA al fondo
    state.scale.forEach((level) => {
        if (level.pm25_min > maxValue) return;
        const top = y(Math.min(level.pm25_max, maxValue));
        const bottom = y(Math.max(0, level.pm25_min));
        ctx.fillStyle = level.color + '12';
        ctx.fillRect(pad.l, top, plotW, Math.max(0, bottom - top));
    });

    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.fillStyle = '#7d5257';
    ctx.font = '10px "Share Tech Mono", monospace';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
        const value = (maxValue / 4) * i;
        const yy = y(value);
        ctx.beginPath();
        ctx.moveTo(pad.l, yy);
        ctx.lineTo(width - pad.r, yy);
        ctx.stroke();
        ctx.fillText(value.toFixed(0), 6, yy + 3);
    }

    const series = (key, color, dash, lineWidth) => {
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = lineWidth;
        ctx.setLineDash(dash);
        ctx.beginPath();
        let drawing = false;
        points.forEach((p, i) => {
            const v = p[key];
            if (v === null || v === undefined) { drawing = false; return; }
            if (!drawing) { ctx.moveTo(x(i), y(v)); drawing = true; }
            else ctx.lineTo(x(i), y(v));
        });
        ctx.stroke();
        ctx.restore();
    };

    // candidatos tenues al fondo, ensamble y observado al frente
    series('linear', 'rgba(255,43,214,0.45)', [4, 4], 1);
    series('cubic', 'rgba(0,240,168,0.45)', [2, 3], 1);
    series('nearest', 'rgba(255,159,28,0.45)', [1, 3], 1);
    series('final', '#fcee0a', [], 2);
    series('clean', '#00f0ff', [], 2.4);

    // marcadores: imputados (amarillo) y huecos irrecuperables (rojo abajo)
    points.forEach((p, i) => {
        if (p.imputed && p.final !== null) {
            ctx.fillStyle = '#fcee0a';
            ctx.beginPath();
            ctx.arc(x(i), y(p.final), 3, 0, Math.PI * 2);
            ctx.fill();
        } else if (p.final === null) {
            ctx.fillStyle = 'rgba(255,0,60,0.85)';
            ctx.fillRect(x(i) - 1, pad.t + plotH - 4, 2, 4);
        }
        if (p.outlier) {
            ctx.strokeStyle = 'rgba(255,0,60,0.7)';
            ctx.beginPath();
            ctx.arc(x(i), pad.t + 6, 3, 0, Math.PI * 2);
            ctx.stroke();
        }
    });

    ctx.fillStyle = '#7d5257';
    ctx.fillText(points[0].timestamp.slice(0, 16).replace('T', ' '), pad.l, height - 8);
    const last = points[points.length - 1].timestamp.slice(0, 16).replace('T', ' ');
    ctx.fillText(last, width - pad.r - ctx.measureText(last).width, height - 8);

    const imputed = points.filter((p) => p.imputed).length;
    const missing = points.filter((p) => p.final === null).length;
    $('chartNote').innerHTML = `Ventana de <b>${points.length} h</b>: `
        + `<b>${points.length - imputed - missing}</b> observadas, `
        + `<b>${imputed}</b> imputadas por el ensamble y `
        + `<b>${missing}</b> irrecuperables. Los circulos rojos arriba son outliers descartados.`;
}

/* ------------------------------------------------------------------ calidad */
function tile(label, value, kind = '') {
    return `<div class="tile ${kind}"><div class="k">${label}</div><div class="v">${value}</div></div>`;
}

async function loadQuality() {
    const q = await api('/api/quality');
    $('maxGap').textContent = q.rules.max_gap_hours;
    $('qualityIndicator').textContent = `${q.indicator} · ${q.availability_raw_pct}% crudo → ${q.availability_final_pct}% final`;
    $('qualityTiles').innerHTML = [
        tile('Registros', q.rows.toLocaleString('es-CO')),
        tile('Estaciones', q.stations),
        tile('Disponibilidad cruda', `${q.availability_raw_pct}%`,
            q.availability_raw_pct > 90 ? 'good' : (q.availability_raw_pct >= 75 ? 'warn' : 'bad')),
        tile('Disponibilidad final', `${q.availability_final_pct}%`, 'good'),
        tile('Nulos de origen', q.missing_source.toLocaleString('es-CO'), q.missing_source ? 'warn' : ''),
        tile('Centinelas', q.sentinels.toLocaleString('es-CO'), q.sentinels ? 'warn' : ''),
        tile('Fuera de rango', q.out_of_range.toLocaleString('es-CO'), q.out_of_range ? 'warn' : ''),
        tile('Bandera SIATA mala', q.bad_quality.toLocaleString('es-CO'), q.bad_quality ? 'warn' : ''),
        tile('Outliers', q.outliers.toLocaleString('es-CO'), q.outliers ? 'warn' : ''),
        tile('Huecos de rejilla', q.gaps.toLocaleString('es-CO'), q.gaps ? 'warn' : ''),
        tile('Imputados', q.imputed.toLocaleString('es-CO'), 'good'),
        tile('Sin recuperar', q.unresolved.toLocaleString('es-CO'), q.unresolved ? 'bad' : 'good'),
    ].join('');

    const body = $('qualityTable').querySelector('tbody');
    body.innerHTML = '';
    q.per_station.forEach((s) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td></td>
            <td>${s.availability_raw_pct}%</td><td>${s.availability_final_pct}%</td>
            <td>${s.missing_source}</td><td>${s.sentinels}</td><td>${s.out_of_range}</td>
            <td>${s.outliers}</td><td>${s.gaps}</td><td>${s.imputed}</td>
            <td>${s.unresolved}</td><td>${s.indicator}</td>`;
        tr.firstElementChild.textContent = `${s.station_code} · ${s.station_name}`;
        body.appendChild(tr);
    });
}

/* ------------------------------------------------------------------ benchmark */
async function loadBenchmark() {
    const button = $('btnBench');
    button.disabled = true;
    button.textContent = 'ejecutando...';
    log('Ocultando datos reales y comparando los cuatro metodos...', 'hl');
    try {
        const data = await api('/api/imputation/benchmark');
        const body = $('benchTable').querySelector('tbody');
        body.innerHTML = '';
        data.summary.forEach((row, index) => {
            const tr = document.createElement('tr');
            if (index === 0) tr.className = 'best';
            tr.innerHTML = `
                <td>${row.method}</td><td>${num(row.mae, 3)}</td><td>${num(row.rmse, 3)}</td>
                <td>${num(row.mape, 2)}</td><td>${num(row.r2, 3)}</td>
                <td>${num(row.impossible_pct, 2)}</td><td>${num(row.time_ms, 2)}</td>
                <td>${row.n.toLocaleString('es-CO')}</td>`;
            body.appendChild(tr);
        });

        const detail = $('benchDetail').querySelector('tbody');
        detail.innerHTML = '';
        data.rows
            .slice()
            .sort((a, b) => a.missing_pct - b.missing_pct || a.method.localeCompare(b.method))
            .forEach((r) => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${r.missing_pct}%</td><td>${r.method}</td><td>${num(r.mae, 3)}</td>
                    <td>${num(r.rmse, 3)}</td><td>${num(r.mape, 2)}</td><td>${num(r.r2, 3)}</td>
                    <td>${r.station_code}</td>`;
                detail.appendChild(tr);
            });
        $('benchDetailWrap').classList.remove('hidden');
        $('benchMethod').innerHTML = `${data.methodology}<br>${data.dataset_note || ''}<br>`
            + 'Estaciones evaluadas: '
            + data.stations.map((s) => `<b>${s.station_code}</b> (${s.availability_pct}%, ${s.points.toLocaleString('es-CO')} puntos)`).join(', ')
            + `. Menor MAE: <b>${data.winner}</b>.`;
        log(`Benchmark listo. Mejor MAE: ${data.winner}`, 'ok');
    } catch (error) {
        log(`Benchmark fallido: ${error.message}`, 'err');
    } finally {
        button.disabled = false;
        button.textContent = 'ejecutar experimento';
    }
}

/* ------------------------------------------------------------------ ETL */
async function runEtl() {
    const button = $('btnEtl');
    const source = $('etlSource').value;
    button.disabled = true;
    button.textContent = 'corriendo...';
    log(`Pipeline ETL lanzado (fuente: ${source})...`, 'hl');
    try {
        const data = await api('/api/etl/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source }),
        });
        log(`Run #${data.run_id} · ${data.source} · ${data.rows.toLocaleString('es-CO')} filas · `
            + `${data.stations} estaciones · ${data.duration_s}s`, 'ok');
        log(`Limpieza: ${data.report.sentinels} centinelas, ${data.report.out_of_range} fuera de rango, `
            + `${data.report.outliers} outliers, ${data.report.gaps} huecos → `
            + `${data.report.imputed} imputados (${data.report.unresolved} sin recuperar)`, '');
        await boot(false);
    } catch (error) {
        log(`Pipeline fallido: ${error.message}`, 'err');
    } finally {
        button.disabled = false;
        button.textContent = 'correr pipeline';
    }
}

async function loadHealth(verbose = true) {
    const health = await api('/api/health');
    $('dbStats').textContent = health.db && health.db.measurements !== undefined
        ? `sqlite · ${health.db.measurements.toLocaleString('es-CO')} mediciones · ${health.db.runs} runs`
        : 'sqlite · --';
    if (health.dataset) setSourceChip(health.dataset);
    if (verbose) {
        log(`${health.app} v${health.version} · pipeline ${health.pipeline_ready ? 'listo' : 'calentando'}`,
            health.pipeline_ready ? 'ok' : 'warn');
        if (health.dataset) {
            log(`Fuente: ${health.dataset.source} (${health.dataset.realtime ? 'tiempo real' : 'historico'}) · `
                + `${health.dataset.rows.toLocaleString('es-CO')} filas · ventana ${health.dataset.window.start} → ${health.dataset.window.end}`, '');
            if (health.dataset.live_error) log(`SIATA en vivo no disponible: ${health.dataset.live_error}`, 'warn');
        }
    }
    return health;
}

/* ------------------------------------------------------------------ arranque */
async function boot(verbose = true) {
    try {
        const scaleData = await api('/api/scale');
        state.scale = scaleData.levels;
        renderScale();
        await loadHealth(verbose);
        const stations = await loadStations();
        await loadQuality();
        if (stations.length) await selectStation(state.selected ?? stations[0].station_id);
    } catch (error) {
        log(`Arranque incompleto: ${error.message}`, 'err');
        setTimeout(() => boot(false), 4000);
    }
}

function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    // Cuando entra a mandar una version nueva del service worker, recargamos una
    // sola vez: sin esto el telefono se queda pegado con la copia vieja en cache
    // despues de cada despliegue.
    // En la primera visita la pagina pasa de "sin controlador" a controlada:
    // eso no es una actualizacion y recargar ahi seria un parpadeo gratis.
    const hadController = Boolean(navigator.serviceWorker.controller);
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (reloading || !hadController) return;
        reloading = true;
        window.location.reload();
    });
    navigator.serviceWorker.register('/sw.js')
        .then((registration) => {
            registration.update().catch(() => {});
            log('Service worker activo: la app queda instalable y disponible offline.', 'ok');
        })
        .catch((error) => log(`Service worker no registrado: ${error.message}`, 'warn'));
}

document.addEventListener('DOMContentLoaded', () => {
    $('btnScan').addEventListener('click', scan);
    $('btnManual').addEventListener('click', scanManual);
    // Sin contexto seguro el GPS nunca va a responder: mostramos ya la entrada manual.
    if (!window.isSecureContext) $('manualBox').classList.remove('hidden');
    $('btnBench').addEventListener('click', loadBenchmark);
    $('btnEtl').addEventListener('click', runEtl);
    $('btnHealth').addEventListener('click', () => loadHealth(true));
    window.addEventListener('online', updateNetChip);
    window.addEventListener('offline', updateNetChip);
    window.addEventListener('resize', () => {
        if (state.history) drawChart(state.history.points);
    });
    updateNetChip();
    tickClock();
    setInterval(tickClock, 1000);
    registerServiceWorker();
    boot();
});

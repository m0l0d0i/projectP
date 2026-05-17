/*
 * FEA-ADMIN-NODE-MONITOR (D12): минимальный Canvas-renderer для probe-графиков
 * на /admin/nodes/{id}. Без внешних зависимостей (strict CSP script-src 'self'
 * в app/web/app.py запрещает Chart.js CDN).
 *
 * Контракт payload (JSON в #node-charts-initial-data, и same shape для AJAX):
 *   { range, range_label, bucket_seconds, since, until,
 *     points: [{ts, latency_ms, users_online, users_total, ok, fail}, ...] }
 *
 * Тики и подписи рисуются server-side downsample'нутыми — клиент ничего не
 * агрегирует. При смене range делает GET /admin/nodes/{id}/samples.json?range=…
 */

(function () {
    'use strict';

    var scriptEl = document.currentScript;
    var nodeId = scriptEl && scriptEl.getAttribute('data-node-id');
    var initialRange = (scriptEl && scriptEl.getAttribute('data-initial-range')) || '24h';

    var initialDataEl = document.getElementById('node-charts-initial-data');
    var initialPayload = null;
    if (initialDataEl) {
        try {
            initialPayload = JSON.parse(initialDataEl.textContent || 'null');
        } catch (err) {
            console.warn('Failed to parse initial chart payload', err);
            initialPayload = null;
        }
    }

    var COLOR_AXIS = '#475569';
    var COLOR_GRID = 'rgba(71, 85, 105, 0.25)';
    var COLOR_TEXT = '#94a3b8';
    var COLOR_LATENCY = '#22d3ee';
    var COLOR_USERS = '#a78bfa';
    var COLOR_OK = '#10b981';
    var COLOR_FAIL = '#f43f5e';
    var COLOR_NO_DATA = '#334155';

    function clear(ctx, w, h) {
        ctx.clearRect(0, 0, w, h);
    }

    function fmtTick(value) {
        if (value >= 1000) {
            return Math.round(value / 100) / 10 + 'k';
        }
        if (Math.abs(value) < 1 && value !== 0) {
            return value.toFixed(2);
        }
        return Math.round(value).toString();
    }

    function fmtTime(iso) {
        var d = new Date(iso);
        if (isNaN(d.getTime())) {
            return iso;
        }
        var hh = String(d.getHours()).padStart(2, '0');
        var mm = String(d.getMinutes()).padStart(2, '0');
        var dd = String(d.getDate()).padStart(2, '0');
        var mo = String(d.getMonth() + 1).padStart(2, '0');
        return dd + '.' + mo + ' ' + hh + ':' + mm;
    }

    function pickValues(points, key) {
        var out = [];
        for (var i = 0; i < points.length; i++) {
            var v = points[i][key];
            if (v !== null && v !== undefined && !isNaN(v)) {
                out.push(Number(v));
            }
        }
        return out;
    }

    function setEmptyHint(name, isEmpty) {
        var hint = document.querySelector('[data-chart-empty-for="' + name + '"]');
        if (!hint) {
            return;
        }
        hint.hidden = !isEmpty;
    }

    function drawLineChart(canvas, points, key, color, emptyName) {
        var ctx = canvas.getContext('2d');
        var w = canvas.width;
        var h = canvas.height;
        clear(ctx, w, h);

        var values = pickValues(points, key);
        if (!points.length || !values.length) {
            setEmptyHint(emptyName, true);
            return;
        }
        setEmptyHint(emptyName, false);

        var padL = 48, padR = 12, padT = 12, padB = 28;
        var plotW = w - padL - padR;
        var plotH = h - padT - padB;

        var maxV = Math.max.apply(null, values);
        var minV = Math.min.apply(null, values);
        if (maxV === minV) {
            maxV = maxV + 1;
            minV = Math.max(0, minV - 1);
        }
        var range = maxV - minV;

        // Y axis ticks (5).
        ctx.strokeStyle = COLOR_GRID;
        ctx.lineWidth = 1;
        ctx.fillStyle = COLOR_TEXT;
        ctx.font = '11px system-ui, sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (var i = 0; i <= 4; i++) {
            var y = padT + (plotH * i) / 4;
            var v = maxV - (range * i) / 4;
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(w - padR, y);
            ctx.stroke();
            ctx.fillText(fmtTick(v), padL - 6, y);
        }

        // X axis ticks (first/middle/last).
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        var nPoints = points.length;
        var xTickIdx = [0, Math.floor(nPoints / 2), nPoints - 1];
        for (var t = 0; t < xTickIdx.length; t++) {
            var idx = xTickIdx[t];
            var x = padL + (plotW * idx) / Math.max(1, nPoints - 1);
            ctx.fillText(fmtTime(points[idx].ts), x, h - padB + 6);
        }

        // Axis lines.
        ctx.strokeStyle = COLOR_AXIS;
        ctx.beginPath();
        ctx.moveTo(padL, padT);
        ctx.lineTo(padL, h - padB);
        ctx.lineTo(w - padR, h - padB);
        ctx.stroke();

        // Line.
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        var inPath = false;
        for (var p = 0; p < nPoints; p++) {
            var raw = points[p][key];
            if (raw === null || raw === undefined || isNaN(raw)) {
                inPath = false;
                continue;
            }
            var num = Number(raw);
            var px = padL + (plotW * p) / Math.max(1, nPoints - 1);
            var py = padT + plotH - (plotH * (num - minV)) / range;
            if (!inPath) {
                ctx.moveTo(px, py);
                inPath = true;
            } else {
                ctx.lineTo(px, py);
            }
        }
        ctx.stroke();
    }

    function drawStatusBar(canvas, points) {
        var ctx = canvas.getContext('2d');
        var w = canvas.width;
        var h = canvas.height;
        clear(ctx, w, h);

        if (!points.length) {
            setEmptyHint('status', true);
            return;
        }
        setEmptyHint('status', false);

        var padL = 8, padR = 8;
        var barH = h - 24;
        var barTop = 6;
        var plotW = w - padL - padR;
        var step = plotW / points.length;

        for (var i = 0; i < points.length; i++) {
            var p = points[i];
            var ok = Number(p.ok || 0);
            var fail = Number(p.fail || 0);
            var color = COLOR_NO_DATA;
            if (fail > 0 && ok === 0) {
                color = COLOR_FAIL;
            } else if (fail > 0 && ok > 0) {
                color = '#f59e0b';
            } else if (ok > 0) {
                color = COLOR_OK;
            }
            ctx.fillStyle = color;
            ctx.fillRect(padL + i * step, barTop, Math.max(1, step - 0.5), barH);
        }

        ctx.fillStyle = COLOR_TEXT;
        ctx.font = '11px system-ui, sans-serif';
        ctx.textBaseline = 'top';
        ctx.textAlign = 'left';
        ctx.fillText(fmtTime(points[0].ts), padL, barTop + barH + 4);
        ctx.textAlign = 'right';
        ctx.fillText(fmtTime(points[points.length - 1].ts), w - padR, barTop + barH + 4);
    }

    function renderAll(payload) {
        var points = (payload && payload.points) || [];
        var latencyCanvas = document.getElementById('node-chart-latency');
        var usersCanvas = document.getElementById('node-chart-users');
        var statusCanvas = document.getElementById('node-chart-status');
        if (latencyCanvas) {
            drawLineChart(latencyCanvas, points, 'latency_ms', COLOR_LATENCY, 'latency');
        }
        if (usersCanvas) {
            drawLineChart(usersCanvas, points, 'users_online', COLOR_USERS, 'users');
        }
        if (statusCanvas) {
            drawStatusBar(statusCanvas, points);
        }
    }

    function updateSwitcherActive(rangeKey) {
        var buttons = document.querySelectorAll('#node-charts-range-switcher button[data-range]');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var active = btn.getAttribute('data-range') === rangeKey;
            btn.className = 'rounded-xl border px-3 py-2 text-sm font-semibold ' + (
                active
                    ? 'border-cyan-500 bg-cyan-500/10 text-cyan-200'
                    : 'border-slate-700 bg-slate-950 text-slate-300 hover:border-cyan-500 hover:text-white'
            );
        }
    }

    function attachRangeSwitcher() {
        var switcher = document.getElementById('node-charts-range-switcher');
        if (!switcher || !nodeId) {
            return;
        }
        switcher.addEventListener('click', function (event) {
            var btn = event.target.closest('button[data-range]');
            if (!btn) {
                return;
            }
            var rangeKey = btn.getAttribute('data-range');
            if (!rangeKey) {
                return;
            }
            updateSwitcherActive(rangeKey);
            fetch('/admin/nodes/' + encodeURIComponent(nodeId) + '/samples.json?range=' + encodeURIComponent(rangeKey), {
                credentials: 'same-origin',
                headers: {'Accept': 'application/json'},
            })
                .then(function (resp) {
                    if (!resp.ok) {
                        throw new Error('HTTP ' + resp.status);
                    }
                    return resp.json();
                })
                .then(function (payload) {
                    renderAll(payload);
                })
                .catch(function (err) {
                    console.warn('Failed to load probe samples', err);
                });
        });
    }

    function init() {
        renderAll(initialPayload);
        attachRangeSwitcher();
        updateSwitcherActive(initialRange);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

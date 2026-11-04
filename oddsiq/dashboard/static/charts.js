/* Shared Chart.js helpers for the OddsIQ dashboard.
 * Loaded after chart.umd.min.js (see base.html). Solves two bugs:
 *   1. htmx re-renders (range toggles, 300s auto-refresh, "Refresh all") used to
 *      call `new Chart()` on a fresh canvas without destroying the prior instance,
 *      leaking a chart each time. oddsiqChart() destroys the previous instance for
 *      a given canvas id first.
 *   2. With Chart.js default maintainAspectRatio:true and no fixed-height parent,
 *      charts self-shrink on each re-render (212->80->40px). We force
 *      maintainAspectRatio:false so height comes from the .chart-box CSS container.
 */
(function () {
  if (!window.Chart) return;

  // Calm data-terminal defaults (the dashboard is always dark): muted axis text,
  // very subtle gridlines, monospace figures to match the table numerals.
  Chart.defaults.color = '#838877';
  Chart.defaults.borderColor = 'rgba(255,255,255,0.05)';
  Chart.defaults.font.family = "'Geist Mono', ui-monospace, monospace";
  Chart.defaults.font.size = 10.5;

  // Per-canvas registry so a re-render destroys the prior instance (no leak).
  var registry = {};

  window.oddsiqChart = function (canvasId, config) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return null;
    if (registry[canvasId]) {
      registry[canvasId].destroy();
      delete registry[canvasId];
    }
    config.options = config.options || {};
    config.options.responsive = true;
    config.options.maintainAspectRatio = false; // height anchored to .chart-box
    var chart = new Chart(canvas.getContext('2d'), config);
    registry[canvasId] = chart;
    return chart;
  };

  // Opt-in plugin: draw each bar's value above it (Chart.js v4 has no built-in datalabels).
  window.oddsiqBarValueLabels = {
    id: 'oddsiqBarValueLabels',
    afterDatasetsDraw: function (chart) {
      var ctx = chart.ctx;
      chart.data.datasets.forEach(function (dataset, di) {
        var meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        meta.data.forEach(function (bar, i) {
          var val = dataset.data[i];
          if (val === null || val === undefined) return;
          ctx.save();
          ctx.fillStyle = Chart.defaults.color;
          ctx.font = '600 12px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'bottom';
          ctx.fillText(String(val), bar.x, bar.y - 4);
          ctx.restore();
        });
      });
    }
  };
})();

(function () {
  function initChecklist() {
    document.querySelectorAll(".workflow-checklist").forEach(function (box, boxIndex) {
      box.querySelectorAll("input[type='checkbox']").forEach(function (item, itemIndex) {
        var key = "fp-doc-check-" + location.pathname + "-" + boxIndex + "-" + itemIndex;
        item.checked = localStorage.getItem(key) === "1";
        var label = item.closest("label");
        if (label) label.classList.toggle("done", item.checked);
        item.addEventListener("change", function () {
          localStorage.setItem(key, item.checked ? "1" : "0");
          if (label) label.classList.toggle("done", item.checked);
        });
      });
    });
  }

  function initSignalLab() {
    var canvas = document.getElementById("signal-canvas");
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    var freq = document.getElementById("signal-freq");
    var reference = document.getElementById("reference-freq");
    var noise = document.getElementById("signal-noise");
    var freqValue = document.getElementById("signal-freq-value");
    var referenceValue = document.getElementById("reference-freq-value");
    var noiseValue = document.getElementById("signal-noise-value");
    var readout = document.getElementById("signal-readout");

    function resize() {
      var ratio = window.devicePixelRatio || 1;
      var rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(320, Math.floor(rect.width * ratio));
      canvas.height = Math.floor(230 * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      draw();
    }

    function deterministicNoise(i) {
      return Math.sin(i * 12.9898) * Math.cos(i * 4.1414);
    }

    function trace(values, width, height, color, offset, scale) {
      ctx.beginPath();
      values.forEach(function (value, i) {
        var x = i / (values.length - 1) * width;
        var y = offset - value * scale;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    function draw() {
      var width = canvas.clientWidth;
      var height = 230;
      var f = Number(freq.value);
      var r = Number(reference.value);
      var n = Number(noise.value);
      var samples = 600;
      var raw = [];
      var mixed = [];
      var sum = 0;
      for (var i = 0; i < samples; i += 1) {
        var t = i / samples;
        var y = Math.sin(2 * Math.PI * f * t) + n * deterministicNoise(i);
        var product = y * Math.sin(2 * Math.PI * r * t);
        raw.push(y);
        sum += product;
        mixed.push(sum / (i + 1));
      }
      ctx.clearRect(0, 0, width, height);
      ctx.strokeStyle = "#e5e7eb";
      ctx.lineWidth = 1;
      [58, 172].forEach(function (y) {
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
      });
      trace(raw, width, height, "#64748b", 58, 34);
      trace(mixed, width, height, "#1f6f43", 172, 62);
      ctx.fillStyle = "#475569";
      ctx.font = "12px sans-serif";
      ctx.fillText("recorded signal", 8, 16);
      ctx.fillStyle = "#1f6f43";
      ctx.fillText("running lock-in response", 8, 130);
      freqValue.textContent = f + " Hz";
      referenceValue.textContent = r + " Hz";
      noiseValue.textContent = n.toFixed(2);
      var response = Math.abs(mixed[mixed.length - 1]);
      readout.textContent = "Final response: " + response.toFixed(3) + (Math.abs(f - r) < 0.5 ? "  - frequencies match" : "  - reference rejects most of this signal");
    }

    [freq, reference, noise].forEach(function (input) {
      input.addEventListener("input", draw);
    });
    window.addEventListener("resize", resize);
    resize();
  }

  document.addEventListener("DOMContentLoaded", function () {
    initChecklist();
    initSignalLab();
  });
})();


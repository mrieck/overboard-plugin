"use strict";

// Ship celebration: a rocket climbs from the bottom and pops into a confetti
// burst, with a brief "Shipped — <launch>" banner. Pure vanilla canvas, no deps
// and no network — matches Overboard's offline/no-CDN ethos. Exposes one global,
// celebrateShip(label), called by app.js after a launch is marked shipped.
//
// It never throws into its caller: any failure just means no confetti, never a
// broken "Mark shipped".

(function () {
  const OVERLAY_ID = "ship-celebrate";
  const BURST_COUNT = 140;
  const CLIMB_MS = 1050;  // rocket bottom -> apex (slower, more graceful climb)
  const FALL_MS = 2600;   // confetti burst lifetime (lingers longer)
  const TOTAL_MS = CLIMB_MS + FALL_MS + 200;

  // Brand palette, read from the CSS custom properties so it tracks the theme.
  function palette() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
    return [
      v("--green", "#3ecf7b"),
      v("--accent", "#4da3ff"),
      v("--amber", "#e0a54a"),
      v("--lvl-4", "#39d353"),
      "#ffffff",
    ];
  }

  function removeExisting() {
    const old = document.getElementById(OVERLAY_ID);
    if (old && old._cleanup) old._cleanup();
    else if (old) old.remove();
  }

  function prefersReducedMotion() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (_) {
      return false;
    }
  }

  // The fading pill naming what shipped. Built with plain DOM; CSS drives the
  // pop-in/fade-out. Returned so the caller can remove it on cleanup.
  function makeBanner(label) {
    const b = document.createElement("div");
    b.className = "ship-banner";
    const txt = label ? "🚀 Shipped — " + label : "🚀 Shipped";
    b.textContent = txt;
    document.body.appendChild(b);
    return b;
  }

  function celebrateShip(label) {
    try {
      removeExisting();

      const banner = makeBanner(label);

      // Reduce-motion: acknowledge with just the banner, skip all the physics.
      if (prefersReducedMotion()) {
        setTimeout(() => banner.remove(), 2200);
        return;
      }

      const colors = palette();
      const canvas = document.createElement("canvas");
      canvas.id = OVERLAY_ID;
      canvas.style.cssText =
        "position:fixed;inset:0;z-index:60;pointer-events:none;";
      document.body.appendChild(canvas);

      const ctx = canvas.getContext("2d");
      let W = 0, H = 0, dpr = 1;
      function resize() {
        dpr = window.devicePixelRatio || 1;
        W = window.innerWidth;
        H = window.innerHeight;
        canvas.width = Math.floor(W * dpr);
        canvas.height = Math.floor(H * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      resize();
      window.addEventListener("resize", resize);

      const cx = W / 2;
      const startY = H + 40;
      const apexY = H * 0.32;
      let confetti = null;      // spawned once, at apex
      const trail = [];         // rocket exhaust puffs
      let raf = 0;
      const t0 = performance.now();

      function cleanup() {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        window.removeEventListener("resize", resize);
        canvas.remove();
        banner.remove();
      }
      canvas._cleanup = cleanup;

      function spawnBurst(x, y) {
        const parts = [];
        for (let i = 0; i < BURST_COUNT; i++) {
          const ang = Math.random() * Math.PI * 2;
          // Bias slightly upward so it fountains before gravity wins.
          const speed = 2 + Math.random() * 5;
          parts.push({
            x, y,
            vx: Math.cos(ang) * speed,
            vy: Math.sin(ang) * speed - 2.4,
            g: 0.08 + Math.random() * 0.05,
            size: 4 + Math.random() * 5,
            color: colors[(Math.random() * colors.length) | 0],
            rot: Math.random() * Math.PI,
            vrot: (Math.random() - 0.5) * 0.4,
          });
        }
        return parts;
      }

      function easeOut(t) { return 1 - Math.pow(1 - t, 2); }

      function frame(now) {
        const t = now - t0;
        ctx.clearRect(0, 0, W, H);

        if (t < CLIMB_MS) {
          // Rocket climbing.
          const p = easeOut(t / CLIMB_MS);
          const ry = startY + (apexY - startY) * p;
          trail.push({ x: cx + (Math.random() - 0.5) * 6, y: ry + 16, life: 1 });
          for (const puff of trail) {
            puff.life -= 0.045;
            if (puff.life <= 0) continue;
            ctx.globalAlpha = Math.max(0, puff.life) * 0.6;
            ctx.fillStyle = colors[2]; // amber exhaust
            ctx.beginPath();
            ctx.arc(puff.x, puff.y, 5 * puff.life + 1, 0, Math.PI * 2);
            ctx.fill();
          }
          ctx.globalAlpha = 1;
          ctx.font = "34px serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText("🚀", cx, ry);
        } else {
          if (!confetti) confetti = spawnBurst(cx, apexY);
          const ft = t - CLIMB_MS;
          const fade = Math.max(0, 1 - ft / FALL_MS);
          for (const q of confetti) {
            q.vy += q.g;
            q.x += q.vx;
            q.y += q.vy;
            q.rot += q.vrot;
            ctx.globalAlpha = fade;
            ctx.fillStyle = q.color;
            ctx.save();
            ctx.translate(q.x, q.y);
            ctx.rotate(q.rot);
            ctx.fillRect(-q.size / 2, -q.size / 2, q.size, q.size * 0.6);
            ctx.restore();
          }
          ctx.globalAlpha = 1;
        }

        if (t >= TOTAL_MS) { cleanup(); return; }
        raf = requestAnimationFrame(frame);
      }
      raf = requestAnimationFrame(frame);
    } catch (_) {
      // Celebration is cosmetic — never let it break the ship action.
    }
  }

  window.celebrateShip = celebrateShip;
})();

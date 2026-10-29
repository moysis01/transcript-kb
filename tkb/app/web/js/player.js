import { $ } from "./core.js";

/* The embedded YouTube player: one per video page, seekable from timestamps. */
/* ---------- YouTube player ---------- */
export const Player = {
  vid: null, yt: null, ready: false, pending: null, onTime: null, timer: null,
  load() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    if (!this._p) {
      this._p = new Promise((resolve) => {
        window.onYouTubeIframeAPIReady = resolve;
        const s = document.createElement("script");
        s.src = "https://www.youtube.com/iframe_api";
        document.head.appendChild(s);
      });
    }
    return this._p;
  },
  async mount(vid, start) {
    this.vid = vid;
    this.pending = start || null;
    await this.load();
    if (this.vid !== vid || !$("#player")) return;
    this.yt = new YT.Player("player", {
      videoId: vid,
      playerVars: { start: Math.floor(start || 0), rel: 0, modestbranding: 1, playsinline: 1 },
      events: {
        onReady: () => { this.ready = true; if (this.pending) { this.seek(this.pending, false); this.pending = null; } },
        onError: () => { const f = $("#fallback"); if (f) f.hidden = false; },
      },
    });
    this.timer = setInterval(() => {
      if (this.ready && this.onTime && this.yt.getPlayerState?.() === 1) this.onTime(this.yt.getCurrentTime());
    }, 400);
  },
  seek(t, play = true) {
    const link = $("#fallbackLink");
    if (link) link.href = `https://youtu.be/${this.vid}?t=${Math.floor(t)}`;
    if (!this.ready) { this.pending = t; return; }
    this.yt.seekTo(t, true);
    if (play) this.yt.playVideo();
    this.onTime?.(t);
  },
  reset() {
    clearInterval(this.timer);
    try { this.yt?.destroy(); } catch {}
    Object.assign(this, { vid: null, yt: null, ready: false, pending: null, onTime: null, timer: null });
  },
};

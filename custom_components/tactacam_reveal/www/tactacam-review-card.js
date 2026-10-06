/*
 * Trailwatch Review card: sort "Needs review" trail camera photos.
 *
 * Shipped with the Tactacam Reveal integration, which loads this file
 * automatically. Add it from the dashboard card picker ("Trailwatch Review") or
 * with YAML: `type: custom:tactacam-review-card`. It finds the integration's
 * review entities by their translation keys, so no configuration is needed.
 */

const PLATFORM = "tactacam_reveal";
const KEYS = {
  photo: "review_photo",
  details: "review_details",
  sortAs: "review_sort_as",
  newLabel: "review_new_label",
  previous: "review_previous",
  next: "review_next",
  empty: "review_empty",
  reference: "review_reference",
  askAi: "review_ask_ai",
  queue: "photos_to_review",
};

class TactacamReviewCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { title: "Trail camera review", ...config };
    this._signature = null;
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._ids) this._ids = this._findEntities(hass);
    const signature = this._stateSignature();
    if (signature === this._signature) return;
    this._signature = signature;
    this._render();
  }

  _findEntities(hass) {
    const ids = {};
    for (const [entityId, entry] of Object.entries(hass.entities || {})) {
      if (entry.platform !== PLATFORM || !entry.translation_key) continue;
      for (const [name, key] of Object.entries(KEYS)) {
        if (entry.translation_key === key && !ids[name]) ids[name] = entityId;
      }
    }
    return ids;
  }

  _state(name) {
    const id = this._ids && this._ids[name];
    return id ? this._hass.states[id] : undefined;
  }

  _stateSignature() {
    const details = this._state("details");
    const photo = this._state("photo");
    return JSON.stringify([
      details && details.state,
      details && details.attributes,
      photo && photo.attributes.entity_picture,
      photo && photo.state,
      this._busy,
      this._flash,
      this._error,
      this._videoUrl,
    ]);
  }

  async _call(domain, service, name, data = {}, done = null) {
    const entityId = this._ids[name];
    if (!entityId) return;
    this._busy = true;
    this._error = null;
    this._flash = null;
    this._videoUrl = null;
    this._rerender();
    try {
      await this._hass.callService(domain, service, { entity_id: entityId, ...data });
      this._flash = done;
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._busy = false;
    this._rerender();
    if (this._flash) {
      const shown = this._flash;
      setTimeout(() => {
        if (this._flash === shown) {
          this._flash = null;
          this._rerender();
        }
      }, 3000);
    }
  }

  _rerender() {
    this._signature = null;
    if (this._hass) this.hass = this._hass;
  }

  _press(name, done = null) {
    return this._call("button", "press", name, {}, done);
  }

  _sortAs(label) {
    return this._call("select", "select_option", "sortAs", { option: label }, `Sorted as ${cap(label)}`);
  }

  _newLabel(value) {
    if (value && value.trim()) {
      const label = value.trim();
      return this._call("text", "set_value", "newLabel", { value: label }, `Sorted as ${label}`);
    }
  }

  async _playVideo(mediaContentId) {
    try {
      const resolved = await this._hass.callWS({
        type: "media_source/resolve_media",
        media_content_id: mediaContentId.replace(/_photo\.jpg$/, "_video.mp4"),
      });
      this._videoUrl = resolved.url;
    } catch (err) {
      this._error = `Video: ${err.message || err}`;
    }
    this._rerender();
  }

  _render() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.addEventListener("keydown", (ev) => this._onKey(ev));
      this.tabIndex = 0;
    }
    const details = this._state("details");
    const photo = this._state("photo");
    const title = this._config.title;

    if (!details || !photo) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card header="${esc(title)}"><div class="empty">
        Tactacam Reveal review entities not found. Update the integration to 0.10.0 or later.</div></ha-card>`;
      return;
    }
    const a = details.attributes;
    if (!a.file) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card header="${esc(title)}">
        <div class="empty">Nothing to review. Every photo is sorted.</div></ha-card>`;
      return;
    }
    const labels = (a.labels || []).filter((l) => l !== "human");
    const guess = a.guess ? `<b>${esc(a.guess)}</b>` : "No guess";
    const reason = a.reason ? ` · ${esc(a.reason)}` : "";
    const by = a.decided_by ? ` <span class="by">(${esc(a.decided_by === "ai" ? "AI" : a.decided_by)})</span>` : "";
    const busy = this._busy || a.asking_ai;

    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card>
        <div class="head">
          <div class="title">${esc(title)}</div>
          <div class="pos">${esc(details.state)}</div>
        </div>
        <div class="meta">${esc(a.camera || "")}${a.captured ? " · " + esc(a.captured) : ""}</div>
        <div class="media ${busy ? "dim" : ""}">
          ${this._videoUrl
            ? `<video src="${esc(this._videoUrl)}" controls autoplay playsinline></video>`
            : `<img src="${esc(imageUrl(photo))}" alt="Photo to review">`}
          ${a.has_video && !this._videoUrl ? `<button class="play" data-act="video">▶ Play video</button>` : ""}
          ${busy ? `<div class="spinner">${a.asking_ai ? "Asking AI…" : "Working…"}</div>` : ""}
        </div>
        <div class="guess">
          <span>${guess}${by}${reason}</span>
          ${this._flash ? `<span class="flash">✓ ${esc(this._flash)}</span>` : ""}
        </div>
        ${this._error ? `<div class="error">${esc(this._error)}</div>` : ""}
        <div class="section">Sort as</div>
        <div class="chips">
          <button class="chip empty-chip" data-act="empty" title="E">Empty</button>
          <button class="chip" data-label="human">Human</button>
          ${labels.map((l) => `<button class="chip" data-label="${esc(l)}">${esc(cap(l))}</button>`).join("")}
        </div>
        <div class="new">
          <input type="text" placeholder="Other, e.g. fox or deer, human" maxlength="60">
          <button data-act="new">Sort</button>
        </div>
        <div class="actions">
          <button data-act="previous" title="Left arrow">‹ Previous</button>
          <button data-act="next" title="Right arrow">Skip ›</button>
          <button data-act="reference" title="Empty scene photo the AI compares against">Use as reference</button>
          <button data-act="ask" title="Ask the stronger AI">Ask AI</button>
        </div>
      </ha-card>`;

    const root = this.shadowRoot;
    root.querySelectorAll("[data-label]").forEach((el) =>
      el.addEventListener("click", () => this._sortAs(el.dataset.label))
    );
    const input = root.querySelector(".new input");
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") this._newLabel(input.value);
    });
    const acts = {
      empty: () => this._press("empty", "Marked empty"),
      previous: () => this._press("previous"),
      next: () => this._press("next"),
      reference: () => this._press("reference", "Saved as reference photo"),
      ask: () => this._press("askAi"),
      new: () => this._newLabel(input.value),
      video: () => this._playVideo(a.media_content_id),
    };
    root.querySelectorAll("[data-act]").forEach((el) =>
      el.addEventListener("click", () => acts[el.dataset.act]())
    );
  }

  _onKey(ev) {
    if (this._busy) return;
    if (ev.key === "ArrowLeft") this._press("previous");
    else if (ev.key === "ArrowRight") this._press("next");
    else if (ev.key === "e" || ev.key === "E") this._press("empty", "Marked empty");
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// The image entity keeps the same URL for every photo; its state (the time the
// photo changed) makes the URL unique so the browser does not show a cached one.
function imageUrl(photo) {
  const url = photo.attributes.entity_picture || "";
  return url ? `${url}${url.includes("?") ? "&" : "?"}v=${encodeURIComponent(photo.state)}` : "";
}

function cap(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const STYLE = `<style>
  :host { display: block; max-width: 1000px; margin: 0 auto; }
  ha-card { padding: 16px; outline: none; }
  .head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .title { font-size: 1.25em; font-weight: 500; }
  .pos, .meta, .by { color: var(--secondary-text-color); }
  .meta { margin: 2px 0 12px; }
  /* Fixed 16:9 box (Reveal photos are 1280x720), so the buttons below never
     jump while the next photo loads. */
  .media { position: relative; background: #000; border-radius: 8px; overflow: hidden;
    aspect-ratio: 16 / 9; max-height: 70vh; margin: 0 auto; }
  .media img, .media video { display: block; width: 100%; height: 100%; object-fit: contain; }
  .media.dim img, .media.dim video { opacity: 0.4; }
  .play { position: absolute; right: 8px; bottom: 8px; }
  .spinner { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    color: #fff; font-weight: 500; }
  .guess { margin: 12px 0 4px; line-height: 1.4; display: flex; justify-content: space-between;
    align-items: baseline; gap: 12px; }
  .error { color: var(--error-color); margin: 8px 0; }
  /* Same line as the guess, so the sort buttons never move while sorting. */
  .flash { color: var(--success-color, #43a047); white-space: nowrap; }
  .section { margin: 12px 0 6px; font-weight: 500; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .new, .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .new input { flex: 1; min-width: 160px; padding: 8px; border-radius: 8px;
    border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color); font: inherit; }
  button { font: inherit; cursor: pointer; padding: 8px 12px; border-radius: 18px;
    border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--primary-text-color); }
  button:hover { border-color: var(--primary-color); }
  .chip { padding: 6px 12px; }
  .empty-chip { background: var(--primary-color); color: var(--text-primary-color, #fff);
    border-color: var(--primary-color); }
  .play { background: rgba(0, 0, 0, 0.6); color: #fff; border-color: transparent; }
  .empty { padding: 8px 0; color: var(--secondary-text-color); }
</style>`;

/*
 * Photo page opened by phone alerts: /tactacam-photo?camera=<folder>&file=<name>.
 * Registered by the integration as a panel without a sidebar entry.
 */
class TactacamPhotoPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    const query = window.location.search;
    if (query !== this._query) {
      this._query = query;
      this._load();
    }
  }

  set narrow(value) {}
  set route(value) {}
  set panel(value) {}

  async _load() {
    const params = new URLSearchParams(this._query);
    this._params = { camera: params.get("camera") || "", file: params.get("file") || "" };
    this._photo = null;
    this._error = null;
    this._note = null;
    this._showVideo = false;
    this._render();
    try {
      this._photo = await this._hass.callWS({ type: "tactacam_reveal/photo", ...this._params });
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }

  async _file() {
    const response = await fetch(this._photo.photo_url);
    const blob = await response.blob();
    return new File([blob], this._photo.file.replace(/\.jpg_photo\.jpg$/, ".jpg"), { type: "image/jpeg" });
  }

  // One button: the iPhone share sheet offers both sharing and "Save Image".
  async _share() {
    const p = this._photo;
    const text = `${p.camera}: ${p.category}${p.captured ? " (" + p.captured + ")" : ""}`;
    try {
      const file = await this._file();
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: text, text });
      } else if (navigator.share) {
        await navigator.share({ title: text, text });
      } else {
        this._flash("Sharing is not available in this browser");
      }
    } catch (err) {
      if (err.name !== "AbortError") this._flash(`Could not share: ${err.message || err}`);
    }
  }

  async _requestVideo() {
    try {
      const result = await this._hass.callWS({ type: "tactacam_reveal/request_video", ...this._params });
      this._photo.video = result.video;
      this._flash(
        result.video === "requested"
          ? "Video requested. It usually arrives within a few minutes."
          : "Video request queued behind another request."
      );
    } catch (err) {
      this._flash(err.message || String(err));
    }
  }

  _close() {
    if (window.history.length > 1) {
      window.history.back();
    } else {
      window.history.replaceState(null, "", "/");
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: true } }));
    }
  }

  _flash(text) {
    this._note = text;
    this._render();
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const p = this._photo;
    let body;
    if (this._error) {
      body = `<div class="message">${esc(this._error)}</div>`;
    } else if (!p) {
      body = `<div class="message">Loading…</div>`;
    } else {
      const media = this._showVideo && p.video_url
        ? `<video src="${esc(p.video_url)}" controls autoplay playsinline></video>`
        : `<img src="${esc(p.photo_url)}" alt="${esc(p.category)}">`;
      const video = {
        downloaded: `<button data-act="play">${this._showVideo ? "Show photo" : "▶ Play video"}</button>`,
        available: `<button data-act="request">Request video</button>`,
        requested: `<button disabled>Video requested</button>`,
        waiting: `<button disabled>Video queued</button>`,
      }[p.video] || "";
      body = `
        <div class="media">${media}</div>
        <div class="info"><b>${esc(p.category)}</b> · ${esc(p.camera)}${p.captured ? " · " + esc(p.captured) : ""}</div>
        <div class="note">${this._note ? esc(this._note) : ""}</div>
        <div class="actions">
          <button data-act="share">Share/Save</button>
          ${video}
          <button data-act="close">Close</button>
        </div>`;
    }
    this.shadowRoot.innerHTML = `${PANEL_STYLE}
      <div class="bar">
        <button class="x" data-act="close" aria-label="Close">✕</button>
        <div class="title">${p ? esc(p.camera + ": " + p.category) : "Trail camera photo"}</div>
      </div>
      <div class="content">${body}</div>`;
    const acts = {
      share: () => this._share(),
      request: () => this._requestVideo(),
      play: () => { this._showVideo = !this._showVideo; this._render(); },
      close: () => this._close(),
    };
    this.shadowRoot.querySelectorAll("[data-act]").forEach((el) =>
      el.addEventListener("click", () => acts[el.dataset.act]())
    );
  }
}

const PANEL_STYLE = `<style>
  :host { display: block; min-height: 100vh; background: var(--primary-background-color);
    color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, inherit); }
  .bar { display: flex; align-items: center; gap: 8px; height: 56px; padding: 0 8px;
    padding-top: env(safe-area-inset-top); background: var(--app-header-background-color);
    color: var(--app-header-text-color, var(--text-primary-color)); }
  .bar .title { font-size: 20px; }
  .x { background: none; border: none; color: inherit; font-size: 22px; padding: 8px 12px; }
  .content { max-width: 1000px; margin: 0 auto; padding: 16px; }
  .media { background: #000; border-radius: 8px; overflow: hidden; aspect-ratio: 16 / 9; max-height: 70vh; }
  .media img, .media video { display: block; width: 100%; height: 100%; object-fit: contain; }
  .info { margin: 12px 0 4px; }
  .note { min-height: 1.4em; color: var(--secondary-text-color); }
  .actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-top: 8px; }
  .actions button { font: inherit; padding: 12px; border-radius: 12px; cursor: pointer;
    border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color); }
  .actions button:disabled { opacity: 0.6; cursor: default; }
  .message { padding: 32px 0; text-align: center; color: var(--secondary-text-color); }
</style>`;

if (!customElements.get("tactacam-photo-panel")) {
  customElements.define("tactacam-photo-panel", TactacamPhotoPanel);
}

if (!customElements.get("tactacam-review-card")) {
  customElements.define("tactacam-review-card", TactacamReviewCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "tactacam-review-card",
    name: "Trailwatch Review",
    description: "Sort trail camera photos the classifier was unsure about.",
    preview: false,
  });
}

// The action icons of lyrembed's and cdimport's pages, drawn after Lucide (ISC,
// see vendor/LICENSES.txt). This file is the same in both programs (a copy,
// like vendor/): change both.
import { html } from '/static/vendor/preact-htm.js';

const SHAPES = {
  check: () => html`<path d="M20 6 9 17l-5-5"></path>`,
  disc: () => html`<circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="2.5"></circle>`,
  down: () => html`<path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path>`,
  flag: () => html`<path d="M4 22V4"></path><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1"></path>`,
  folder: () => html`<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>`,
  home: () => html`<path d="m3 10 9-7 9 7v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path><path d="M9 22V12h6v10"></path>`,
  image: () => html`<rect x="3" y="3" width="18" height="18" rx="2"></rect><circle cx="9" cy="9" r="2"></circle><path d="m21 15-3-3a2 2 0 0 0-3 0L6 21"></path>`,
  layers: () => html`<path d="m12 2 9 5-9 5-9-5z"></path><path d="m3 12 9 5 9-5"></path><path d="m3 17 9 5 9-5"></path>`,
  left: () => html`<path d="m15 18-6-6 6-6"></path>`,
  link: () => html`<path d="M15 3h6v6"></path><path d="M10 14 21 3"></path><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>`,
  list: () => html`<path d="M8 6h13"></path><path d="M8 12h13"></path><path d="M8 18h13"></path><path d="M3 6h.01"></path><path d="M3 12h.01"></path><path d="M3 18h.01"></path>`,
  lyrics: () => html`<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path><path d="M8 8h8"></path><path d="M8 12h5"></path>`,
  note: () => html`<path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle>`,
  pencil: () => html`<path d="M21.2 6.8a2.8 2.8 0 0 0-4-4L3.8 16.2a2 2 0 0 0-.5.8l-1.3 4.4a.5.5 0 0 0 .6.6l4.4-1.3a2 2 0 0 0 .8-.5z"></path><path d="m15 5 4 4"></path>`,
  power: () => html`<path d="M12 2v10"></path><path d="M18.4 6.6a9 9 0 1 1-12.8 0"></path>`,
  refresh: () => html`<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"></path><path d="M21 3v5h-5"></path>`,
  right: () => html`<path d="m9 18 6-6-6-6"></path>`,
  save: () => html`<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><path d="M17 21v-8H7v8"></path><path d="M7 3v5h8"></path>`,
  search: () => html`<circle cx="11" cy="11" r="7"></circle><path d="m21 21-4.3-4.3"></path>`,
  settings: () => html`<path d="M4 7h9"></path><path d="M19 7h1"></path><circle cx="16" cy="7" r="3"></circle><path d="M4 17h1"></path><path d="M11 17h9"></path><circle cx="8" cy="17" r="3"></circle>`,
  stop: () => html`<rect x="5" y="5" width="14" height="14" rx="2"></rect>`,
  tag: () => html`<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"></path><circle cx="7.5" cy="7.5" r="1"></circle>`,
  tools: () => html`<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"></path>`,
  trash: () => html`<path d="M3 6h18"></path><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>`,
  type: () => html`<path d="M4 7V4h16v3"></path><path d="M9 20h6"></path><path d="M12 4v16"></path>`,
  undo: () => html`<path d="M3 7v6h6"></path><path d="M21 17a9 9 0 0 0-15-6.7L3 13"></path>`,
  up: () => html`<path d="m18 15-6-6-6 6"></path>`,
  x: () => html`<path d="M18 6 6 18"></path><path d="m6 6 12 12"></path>`,
};

export function Icon({ name, size = 16 }) {
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SHAPES[name]()}</svg>`;
}

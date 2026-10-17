# docs/

GitHub Pages site, served from `/docs` on `main` at
<https://theroxat.github.io/DJ_Snake/>.

- `index.html`: landing page.
- `demo/`: the real clip page (`web/index.html`) running against a fake API.
  - `shim.js` replaces `window.fetch` for `/api/{sid}/state`, `/control` and
    `/theme`, with fictional tracks and in-memory state. `?edit=demo` opens the
    theme editor (saving only updates the in-memory theme; logos are refused).
  - `index.html` is generated, do not edit it. After changing `web/index.html`
    (or `shim.js`), regenerate it with `python3 docs/demo/build.py` and commit.
  - `media/*.webm` are tiny generated clips, rebuilt by `make-media.sh` (needs
    ffmpeg, or the `dj-snake` Docker image) only if you want new visuals.
- `screenshots/`: images used by the README and the landing page
  (`demo.webp` is an animated WebP; `themes.png` is the theme grid).
- `social-preview.png`: 1280x640 image to upload by hand in the repository
  settings (Social preview).
- `.nojekyll`: tells Pages to serve files as they are.

All data in the demo and the images is fictional.

To test locally with video seeking, use a server that supports HTTP Range
requests (GitHub Pages does; `python3 -m http.server` does not).

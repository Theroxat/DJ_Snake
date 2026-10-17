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
  - `media/*.webm` are 25 s excerpts of real CC-BY films (with `thumb*.jpg`), rebuilt by
    `make-media.sh` (needs the source videos, ffmpeg or the `dj-snake` image).
- `screenshots/`: images used by the README and the landing page
  (`demo.webp` is an animated WebP (1280x800, 15 fps) and `demo.mp4` its H.264 twin; `themes.png` is the theme grid).
- `social-preview.png`: 1280x640 image to upload by hand in the repository
  settings (Social preview).
- `.nojekyll`: tells Pages to serve files as they are.

The queue data in the demo is fictional; the footage is real (see below).

To test locally with video seeking, use a server that supports HTTP Range
requests (GitHub Pages does; `python3 -m http.server` does not).

## Demo footage credits

The demo, the screenshots and the social preview use 25 s excerpts (cropped to 16:9, resized, no audio) of these films, released under the Creative Commons Attribution licence. Thumbnails are the official YouTube thumbnails. Excerpts: `demo/make-media.sh`.

- [Caminandes 3: Llamigos](https://www.youtube.com/watch?v=SkVqJ1SGeL0), (c) Blender Institute / Pablo Vazquez, CC BY (YouTube licence field).
- [Cosmos Laundromat - First Cycle](https://www.youtube.com/watch?v=Y-rmzh0PI3c), (c) Blender Foundation, CC BY (YouTube licence field).
- [Big Buck Bunny](https://www.youtube.com/watch?v=aqz-KE-bpKQ), (c) Blender Foundation, CC BY (YouTube licence field).
- [Tears of Steel](https://www.youtube.com/watch?v=R6MlUcmOul8), (c) Blender Foundation, CC BY 3.0 (stated in the video description, tearsofsteel.org).
- [Sintel](https://www.youtube.com/watch?v=eRsGyueVLvQ), (c) Blender Foundation, CC BY 3.0 (durian.blender.org/sharing).

Licence: <https://creativecommons.org/licenses/by/3.0/>. The films are not endorsed by DJ Snake and DJ Snake is not endorsed by Blender.

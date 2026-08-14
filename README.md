# JobPrep Works — Open Edition · project site

This branch is the GitHub Pages site, served at
<https://calvincs.github.io/JobPrepWorks-OpenEdition/>.

It is a single static page — no build step, no generator, no dependencies.

```
index.html          the page
assets/site.css     theming, lifted from the JobPrep Studio landing page
assets/site.js      scroll reveal (respects prefers-reduced-motion)
assets/fonts/       Geist, Geist Mono, Anton (SIL OFL, self-hosted)
assets/shots/       screenshots of the running app
.nojekyll           serve files verbatim; skip Jekyll processing
```

To update: edit `index.html`, commit, push. Screenshots are captured from the
app at 1440px wide.

This page loads Google Analytics (`G-7D1G40BK7R`) — unconditionally, with no
consent banner. The application on `main` has no analytics of any kind, and the
page says so explicitly so the two aren't confused.

The application source lives on the `main` branch.

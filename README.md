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

The application source lives on the `main` branch.

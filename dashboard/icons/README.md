Drop the app icon files here (PNG, square, no transparency needed):

- icon-192.png            192×192   Android / browsers
- icon-512.png            512×512   Android splash + install
- icon-512-maskable.png   512×512   same art with ~20% safe padding (Android adaptive icons)
- apple-touch-icon.png    180×180   iPhone home screen

The dashboard serves them at /icons/<name>. Until they exist, phones fall back to a plain tile.

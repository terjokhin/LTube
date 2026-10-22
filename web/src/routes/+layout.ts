// Every page is a static shell that loads its data from /api in the browser.
// Trailing slashes make /settings/ prerender to settings/index.html, which the Python static server finds.
export const prerender = true;
export const trailingSlash = 'always';

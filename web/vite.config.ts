import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// The Python package serves the built UI from here (see src/ltube/web/app.py).
const OUT = '../src/ltube/web/static';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			adapter: adapter({ pages: OUT, assets: OUT })
		})
	],
	server: {
		// `npm run dev` talks to `uv run ltube serve` on the default port.
		proxy: { '/api': 'http://127.0.0.1:8000' }
	}
});

// Mirror of render_name in src/ltube/metadata.py, used for live previews only;
// the server renders the real file names. Keep the two in sync.

export const NAME_FIELDS = ['number', 'artist', 'title', 'album', 'year'] as const;
export type NameField = (typeof NAME_FIELDS)[number];

const FIELD_RE = /(\{\w+\})/;

export const EXAMPLE: Record<NameField, string> = {
	number: '01',
	artist: 'Russian Circles',
	title: 'Borehole',
	album: 'Nine',
	year: '2026'
};

/** Fill `pattern`; an empty field disappears together with its separator. */
export function renderName(pattern: string, values: Partial<Record<string, string>>): string {
	const tokens = pattern.split(FIELD_RE); // literal, field, literal, ..., literal
	const parts = [tokens[0]];
	let haveValue = false;
	for (let i = 1; i < tokens.length; i += 2) {
		const after = tokens[i + 1] ?? '';
		const value = values[tokens[i].slice(1, -1)];
		if (value) {
			parts.push(value, after);
			haveValue = true;
		} else if (haveValue) {
			const before = parts[parts.length - 1].trimEnd();
			const close = after.trimStart();
			if (['(', '['].includes(before.slice(-1)) && [')', ']'].includes(close.slice(0, 1))) {
				parts[parts.length - 1] = close.slice(1); // drop the whole "(...)"
			} else {
				parts[parts.length - 1] = after; // drop the separator before the empty field
			}
		}
	}
	return parts
		.join('')
		.trim()
		.replace(/^[-_ ]+|[-_ ]+$/g, '');
}

/** Client-side hint; the server has the final say when saving. */
export function patternProblem(pattern: string): string | null {
	if (!pattern.includes('{title}')) return 'Must contain {title}';
	const unknown = [...pattern.matchAll(/\{(\w+)\}/g)]
		.map((m) => m[1])
		.find((f) => !(NAME_FIELDS as readonly string[]).includes(f));
	if (unknown) return `Unknown placeholder {${unknown}}`;
	if (/[/\\]/.test(pattern)) return "Can't contain slashes";
	return null;
}

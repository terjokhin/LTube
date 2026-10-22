<script lang="ts">
	import { api, PACING_FIELDS, type Settings } from '#lib/api.ts';
	import { datetime } from '#lib/format.ts';
	import PatternField from '#lib/PatternField.svelte';
	import { patternProblem } from '#lib/pattern.ts';
	import { settings } from '#lib/state.svelte.ts';

	const SECTION_FIELDS = {
		folders: ['out_dir', 'use_subfolders', 'albums_subfolder', 'playlists_subfolder'],
		names: ['album_pattern', 'playlist_pattern'],
		resync: ['delete_removed'],
		pacing: PACING_FIELDS
	} as const;
	type Section = keyof typeof SECTION_FIELDS;
	type Draft = Pick<Settings, (typeof SECTION_FIELDS)[Section][number]>;

	// Editable copy of the server settings, filled once they arrive.
	let draft = $state<Draft | null>(null);
	$effect(() => {
		if (draft === null && settings.value) draft = pick(settings.value);
	});

	function pick(s: Settings): Draft {
		const keys = Object.values(SECTION_FIELDS).flat();
		return Object.fromEntries(keys.map((k) => [k, s[k]])) as Draft;
	}

	function changed(section: Section): boolean {
		const saved = settings.value;
		return !!draft && !!saved && SECTION_FIELDS[section].some((k) => draft![k] !== saved[k]);
	}

	// An emptied number field binds as null; don't let that be saved.
	const pacingComplete = $derived(
		!!draft && PACING_FIELDS.every((k) => k === 'rate_limit' || typeof draft![k] === 'number')
	);
	const pacingIsDefault = $derived(
		!!draft && !!settings.value && PACING_FIELDS.every((k) => draft![k] === settings.value!.pacing_defaults[k])
	);

	function resetPacing() {
		if (!draft || !settings.value) return;
		Object.assign(draft, settings.value.pacing_defaults);
	}

	let saving = $state<Section | null>(null);
	let sectionError = $state<Partial<Record<Section, string>>>({});
	let sectionNotice = $state<Partial<Record<Section, string>>>({});

	async function save(section: Section, event: SubmitEvent) {
		event.preventDefault();
		if (!draft) return;
		saving = section;
		sectionError[section] = sectionNotice[section] = undefined;
		try {
			const update = Object.fromEntries(SECTION_FIELDS[section].map((k) => [k, draft![k]]));
			settings.value = await api.saveSettings(update);
			// Take the server's normalised values for this section; keep unsaved edits elsewhere.
			for (const k of SECTION_FIELDS[section]) (draft as Record<string, unknown>)[k] = settings.value[k];
			sectionNotice[section] = 'Saved.';
		} catch (e) {
			sectionError[section] = (e as Error).message;
		} finally {
			saving = null;
		}
	}

	const locked = $derived(settings.value?.out_dir_locked ?? false);
	const patternsValid = $derived(
		!!draft && !patternProblem(draft.album_pattern) && !patternProblem(draft.playlist_pattern)
	);

	// Cookies
	let input: HTMLInputElement;
	let file = $state<File | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let notice = $state<string | null>(null);
	const cookies = $derived(settings.value?.cookies);

	async function upload(event: SubmitEvent) {
		event.preventDefault();
		if (!file) return;
		busy = true;
		error = notice = null;
		try {
			const res = await api.uploadCookies(file);
			notice = `Saved ${res.loaded} cookies.`;
			file = null;
			input.value = '';
			await settings.load();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	async function remove() {
		busy = true;
		error = notice = null;
		try {
			await api.deleteCookies();
			await settings.load();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}
</script>

<h1>Settings</h1>

{#if draft}
	<form class="card stack" onsubmit={(e) => save('folders', e)}>
		<h2>Download folder</h2>
		<input
			type="text"
			required
			placeholder="/Users/me/Music"
			bind:value={draft.out_dir}
			readonly={locked}
			aria-label="Download folder"
		/>
		{#if locked}
			<p class="muted small">
				Fixed by the server: in Docker this is where your music folder is mounted. To use a different
				folder, change the volume mount (<code>MUSIC_DIR</code> in compose).
			</p>
		{/if}

		<label class="check">
			<input type="checkbox" bind:checked={draft.use_subfolders} />
			Use subfolders for albums and playlists
		</label>
		{#if draft.use_subfolders}
			<div class="pair">
				<label>
					<span>Albums subfolder</span>
					<input type="text" required bind:value={draft.albums_subfolder} />
				</label>
				<label>
					<span>Playlists subfolder</span>
					<input type="text" required bind:value={draft.playlists_subfolder} />
				</label>
			</div>
		{/if}
		<p class="muted small">
			Albums go to <code
				>{draft.out_dir}/{#if draft.use_subfolders}{draft.albums_subfolder || '…'}/{/if}Artist - Year - Album/</code
			><br />
			Playlists go to <code
				>{draft.out_dir}/{#if draft.use_subfolders}{draft.playlists_subfolder || '…'}/{/if}Playlist name/</code
			>
		</p>

		<div class="actions">
			<button class="primary" type="submit" disabled={!changed('folders') || saving === 'folders'}>Save</button>
			{#if sectionError.folders}<span class="error">{sectionError.folders}</span>{/if}
			{#if sectionNotice.folders && !changed('folders')}<span class="ok">{sectionNotice.folders}</span>{/if}
		</div>
	</form>

	<form class="card stack" onsubmit={(e) => save('names', e)}>
		<h2>File names</h2>
		<p class="muted small">
			Placeholders with no value for a track (say, no year) are dropped along with their separator.
			Changing a pattern doesn't re-download anything: files you already have are recognised by their
			YouTube ID and keep their old names.
		</p>
		<PatternField label="Album tracks" bind:value={draft.album_pattern} />
		<PatternField label="Playlist tracks" bind:value={draft.playlist_pattern} />
		<div class="actions">
			<button
				class="primary"
				type="submit"
				disabled={!changed('names') || !patternsValid || saving === 'names'}
			>
				Save
			</button>
			{#if sectionError.names}<span class="error">{sectionError.names}</span>{/if}
			{#if sectionNotice.names && !changed('names')}<span class="ok">{sectionNotice.names}</span>{/if}
		</div>
	</form>

	<form class="card stack" onsubmit={(e) => save('resync', e)}>
		<h2>Resync</h2>
		<label class="check">
			<input type="checkbox" bind:checked={draft.delete_removed} />
			Also delete downloaded files if they were removed from playlist
		</label>
		<p class="muted small">
			Applies to resyncs: a favourite's Resync button, or downloading a playlist again. Only files ltube
			downloaded (recognised by their YouTube ID) are deleted, and only after a complete resync. Nothing is deleted if YouTube hid unavailable videos from the listing, or if more than half
			of the playlist's tracks would go at once.
		</p>
		<div class="actions">
			<button class="primary" type="submit" disabled={!changed('resync') || saving === 'resync'}>
				Save
			</button>
			{#if sectionError.resync}<span class="error">{sectionError.resync}</span>{/if}
			{#if sectionNotice.resync && !changed('resync')}<span class="ok">{sectionNotice.resync}</span>{/if}
		</div>
	</form>

	<form class="card stack" onsubmit={(e) => save('pacing', e)}>
		<h2>Pacing</h2>
		<p class="muted small">
			Pauses and retries keep YouTube from flagging your account as a bot. The defaults are a safe
			balance; changes apply to downloads started after saving.
		</p>
		<div class="grid">
			<div class="field wide">
				<span class="label">Pause between tracks</span>
				<div class="unit">
					<input type="number" min="0" step="1" required aria-label="Pause between tracks, minimum seconds" bind:value={draft.sleep_min} />
					<span>to</span>
					<input type="number" min="0" step="1" required aria-label="Pause between tracks, maximum seconds" bind:value={draft.sleep_max} />
					<span>s</span>
				</div>
				<small class="muted">A random wait in this range after every downloaded or failed track</small>
			</div>
			<label class="field">
				<span class="label">Pause between requests</span>
				<span class="unit"><input type="number" min="0" step="0.5" required bind:value={draft.request_sleep} /><span>s</span></span>
				<small class="muted">After quick lookups, and between yt-dlp's requests within a track</small>
			</label>
			<label class="field">
				<span class="label">Bandwidth limit</span>
				<input type="text" placeholder="unlimited" bind:value={draft.rate_limit} />
				<small class="muted">Per second, e.g. 2M or 500K; empty = unlimited</small>
			</label>
			<label class="field">
				<span class="label">Retries per track</span>
				<input type="number" min="0" step="1" required bind:value={draft.retries} />
				<small class="muted">After the first attempt fails</small>
			</label>
			<label class="field">
				<span class="label">Stop after failures in a row</span>
				<input type="number" min="1" step="1" required bind:value={draft.max_consecutive_failures} />
				<small class="muted">Usually means YouTube is blocking this session</small>
			</label>
			<label class="field">
				<span class="label">First retry delay</span>
				<span class="unit"><input type="number" min="0.1" step="1" required bind:value={draft.backoff_base} /><span>s</span></span>
				<small class="muted">Doubles with each retry, slightly randomised</small>
			</label>
			<label class="field">
				<span class="label">Longest retry delay</span>
				<span class="unit"><input type="number" min="0.1" step="1" required bind:value={draft.backoff_max} /><span>s</span></span>
				<small class="muted">Upper bound for the doubling</small>
			</label>
		</div>
		<div class="actions">
			<button
				class="primary"
				type="submit"
				disabled={!changed('pacing') || !pacingComplete || saving === 'pacing'}
			>
				Save
			</button>
			<button type="button" onclick={resetPacing} disabled={pacingIsDefault}>Reset to defaults</button>
			{#if sectionError.pacing}<span class="error">{sectionError.pacing}</span>{/if}
			{#if sectionNotice.pacing && !changed('pacing')}<span class="ok">{sectionNotice.pacing}</span>{/if}
		</div>
	</form>
{:else}
	<p class="muted">Loading…</p>
{/if}

<section class="card">
	<h2>YouTube cookies</h2>
	<p class="muted">
		ltube downloads as your signed-in YouTube account. Export <code>cookies.txt</code> in Netscape format
		from a browser that is signed in to YouTube, then upload it here. It is stored only on the ltube server.
	</p>

	<p>
		{#if !cookies}
			<span class="muted">Loading…</span>
		{:else if cookies.present}
			<span class="ok">● Uploaded</span>
			<span class="muted">{cookies.updated_at ? datetime(cookies.updated_at) : ''}</span>
		{:else}
			<span class="warn">● Not uploaded</span>
		{/if}
	</p>

	<form class="row" onsubmit={upload}>
		<input
			bind:this={input}
			type="file"
			accept=".txt,text/plain"
			onchange={(e) => (file = e.currentTarget.files?.[0] ?? null)}
		/>
		<button class="primary" type="submit" disabled={!file || busy}>
			{cookies?.present ? 'Replace' : 'Upload'}
		</button>
		{#if cookies?.present}
			<button type="button" onclick={remove} disabled={busy}>Remove</button>
		{/if}
	</form>

	{#if error}<p class="error">{error}</p>{/if}
	{#if notice}<p class="ok">{notice}</p>{/if}
</section>

<style>
	.card + .card {
		margin-top: 1rem;
	}
	h2 {
		margin: 0;
	}
	.stack {
		display: grid;
		gap: 0.8rem;
	}
	.stack p {
		margin: 0;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.pair {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
	}
	.pair label {
		display: grid;
		gap: 0.3rem;
	}
	.pair span {
		font-weight: 600;
		font-size: 0.9rem;
	}
	@media (max-width: 520px) {
		.pair {
			grid-template-columns: 1fr;
		}
	}
	.grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1rem 1.25rem;
	}
	.field {
		display: grid;
		gap: 0.3rem;
		align-content: start;
	}
	.field.wide {
		grid-column: 1 / -1;
	}
	.label {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.unit {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	input[type='number'] {
		width: 6.5rem;
	}
	.field small {
		font-size: 0.8rem;
	}
	@media (max-width: 520px) {
		.grid {
			grid-template-columns: 1fr;
		}
	}
	.actions {
		flex-wrap: wrap;
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.6rem;
	}
	.row input {
		flex: 1;
		min-width: 0;
	}
	input[readonly] {
		color: var(--muted);
	}
	.small {
		font-size: 0.85rem;
	}
	code {
		word-break: break-all;
	}
	.ok {
		color: var(--ok);
	}
	.warn {
		color: var(--warn);
	}
</style>

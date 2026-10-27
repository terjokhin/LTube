<script lang="ts">
	import { api, playlistKey } from '#lib/api.ts';
	import JobCard from '#lib/JobCard.svelte';
	import { live, settings } from '#lib/state.svelte.ts';

	// Remember the checkbox between visits; storage can be unavailable (private mode), so never rely on it.
	const FAVOURITE_KEY = 'ltube.addToFavourites';
	function readFavouritePref(): boolean {
		try {
			return localStorage.getItem(FAVOURITE_KEY) === '1';
		} catch {
			return false;
		}
	}

	let url = $state('');
	// Downloaded before? Then this download is a resync of that folder.
	const known = $derived(url.trim() ? live.playlists.find((p) => p.key === playlistKey(url)) : undefined);
	let favourite = $state(readFavouritePref());
	$effect(() => {
		try {
			localStorage.setItem(FAVOURITE_KEY, favourite ? '1' : '0');
		} catch {
			// ignore
		}
	});
	let error = $state<string | null>(null);
	let submitting = $state(false);

	const hasCookies = $derived(settings.value?.cookies.present ?? false);

	async function start(event: SubmitEvent) {
		event.preventDefault();
		submitting = true;
		error = null;
		try {
			await api.startJob(url, favourite);
			url = '';
		} catch (e) {
			error = (e as Error).message;
		} finally {
			submitting = false;
		}
	}
</script>

<h1>Download a playlist</h1>

{#if settings.value && !hasCookies}
	<p class="banner">
		Upload your <code>cookies.txt</code> in <a href="/settings/">Settings</a> before downloading.
	</p>
{/if}

<form class="card" onsubmit={start}>
	<label>
		<span>Playlist URL</span>
		<input
			type="url"
			required
			placeholder="https://music.youtube.com/playlist?list=…"
			bind:value={url}
		/>
	</label>
	{#if known?.last_synced_at}
		<p class="notice small">
			Downloaded before. This syncs it: only tracks missing from your library are downloaded, and the
			playlist file is updated.
		</p>
	{/if}
	<label class="check">
		<input type="checkbox" bind:checked={favourite} disabled={known?.favourite} />
		{#if known?.favourite}
			Already in <a href="/favourites/">favourites</a>
		{:else}
			Add to <a href="/favourites/">favourites</a> for later resyncs
		{/if}
	</label>
	{#if settings.value}
		{@const s = settings.value}
		<small class="muted">
			Saving to <code>{s.out_dir}/Album Artist/Album/</code>; a playlist also gets
			<code>{s.out_dir}/playlists/&lt;name&gt;.m3u8</code>.
			<a href="/settings/">Change</a>
		</small>
	{/if}
	{#if error}<p class="error">{error}</p>{/if}
	<div>
		<button class="primary" type="submit" disabled={submitting || !hasCookies}>
			{submitting ? 'Starting…' : 'Download'}
		</button>
	</div>
</form>

{#if live.jobs.length}
	<h2>Downloads</h2>
	<div class="jobs">
		{#each live.jobs as job (job.id)}
			<JobCard {job} now={live.now} />
		{/each}
	</div>
{/if}

<style>
	form {
		display: grid;
		gap: 1rem;
	}
	label {
		display: grid;
		gap: 0.3rem;
	}
	.notice {
		margin: 0;
		padding: 0.5rem 0.75rem;
		border-left: 3px solid var(--accent);
		background: var(--bg);
		border-radius: 4px;
	}
	.small {
		font-size: 0.85rem;
	}
	label.check {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	label span {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.error {
		margin: 0;
	}
	.jobs {
		display: grid;
		gap: 0.75rem;
	}
	.banner {
		margin: 0 0 1rem;
	}
</style>

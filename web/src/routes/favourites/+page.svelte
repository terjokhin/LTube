<script lang="ts">
	import { api, type Playlist } from '#lib/api.ts';
	import { datetime } from '#lib/format.ts';
	import { live, settings } from '#lib/state.svelte.ts';

	let errors = $state<Record<string, string>>({});
	let busy = $state<Record<string, boolean>>({});

	const hasCookies = $derived(settings.value?.cookies.present ?? false);
	const countLabels: Record<string, string> = {
		downloaded: 'downloaded',
		'already exists': 'already there',
		unavailable: 'unavailable',
		failed: 'failed',
		moved: 'moved to album'
	};
	const favourites = $derived(live.playlists.filter((p) => p.favourite));

	/** The queued or running download for this favourite, if any. */
	function activeJob(fav: Playlist) {
		return live.jobs.find(
			(j) => j.playlist_id === fav.id && (j.state === 'queued' || j.state === 'running')
		);
	}

	async function act(fav: Playlist, action: () => Promise<unknown>) {
		busy[fav.id] = true;
		delete errors[fav.id];
		try {
			await action();
		} catch (e) {
			errors[fav.id] = (e as Error).message;
		} finally {
			busy[fav.id] = false;
		}
	}

	function remove(fav: Playlist) {
		const name = fav.title ?? fav.url;
		if (!confirm(`Remove "${name}" from favourites? Downloaded files are kept.`)) return;
		act(fav, () => api.setFavourite(fav.id, false));
	}
</script>

<h1>Favourites</h1>
<p class="muted intro">
	Resync downloads only what's missing from your library: tracks added to the playlist since the last
	run, or ones that failed. A track already downloaded for another playlist or album is reused, and the
	playlist file follows the playlist's current order. Tracks removed from the playlist leave the
	playlist file; the files stay.
</p>

{#if settings.value && !hasCookies}
	<p class="banner">
		Upload your <code>cookies.txt</code> in <a href="/settings/">Settings</a> before syncing.
	</p>
{/if}

{#if favourites.length === 0}
	<p class="card muted">
		No favourites yet. Tick <em>Add to favourites</em> when you start a <a href="/">download</a>.
	</p>
{:else}
	<ul>
		{#each favourites as fav (fav.id)}
			{@const job = activeJob(fav)}
			<li class="card">
				<div class="head">
					<div class="title">
						<a class="name" href={fav.url} target="_blank" rel="noreferrer">{fav.title ?? fav.url}</a>
					</div>
					<button
						class="primary"
						onclick={() => act(fav, () => api.syncPlaylist(fav.id))}
						disabled={!!job || busy[fav.id] || !hasCookies}
					>
						Resync
					</button>
					<button onclick={() => remove(fav)} disabled={busy[fav.id]}>Remove</button>
				</div>

				<p class="small">
					{#if job?.state === 'queued'}
						<span class="accent">Queued</span>
					{:else if job}
						<span class="accent">
							Syncing{#if job.total !== null}: {job.processed} / {job.total}{/if}
						</span>
						— progress on the <a href="/">Download</a> page
					{:else if fav.last_synced_at}
						<span class="muted">Last synced {datetime(fav.last_synced_at)}</span>
						{#each Object.entries(fav.last_counts) as [status, n] (status)}
							<span class="count {status.replace(' ', '-')}">· {n} {countLabels[status] ?? status}</span>
						{/each}
						{#if fav.last_state === 'cancelled'}<span class="muted">· cancelled</span>{/if}
					{:else}
						<span class="muted">Never synced</span>
					{/if}
				</p>
				{#if !job && fav.last_error && fav.last_state !== 'cancelled'}
					<p class="small error">{fav.last_error}</p>
				{/if}
				{#if errors[fav.id]}<p class="small error">{errors[fav.id]}</p>{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.intro {
		margin: -0.5rem 0 1rem;
	}
	.banner {
		margin: 0 0 1rem;
	}
	ul {
		display: grid;
		gap: 0.75rem;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.title {
		display: grid;
		flex: 1;
		min-width: 0;
	}
	.title > * {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.name {
		color: var(--text);
		font-weight: 600;
		text-decoration: none;
	}
	.name:hover {
		text-decoration: underline;
	}
	p {
		margin: 0.5rem 0 0;
	}
	.small {
		font-size: 0.85rem;
	}
	.accent {
		color: var(--accent);
		font-weight: 600;
	}
	.count.failed {
		color: var(--bad);
	}
	.count.unavailable {
		color: var(--warn);
	}
	@media (max-width: 520px) {
		.head {
			flex-wrap: wrap;
		}
		.title {
			flex-basis: 100%;
		}
	}
</style>

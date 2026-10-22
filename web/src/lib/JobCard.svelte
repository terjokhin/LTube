<script lang="ts">
	import { api, type Job } from './api';
	import { bytes, seconds } from './format';

	let { job, now }: { job: Job; now: number } = $props();

	let cancelling = $state(false);
	const active = $derived(job.state === 'queued' || job.state === 'running');
	const percent = $derived(job.total ? (job.processed / job.total) * 100 : 0);
	const waitLeft = $derived(job.waiting_until ? job.waiting_until - now : 0);
	const current = $derived(job.current);
	const trackPercent = $derived(
		current?.total_bytes ? ((current.downloaded_bytes ?? 0) / current.total_bytes) * 100 : null
	);

	const stateLabel: Record<Job['state'], string> = {
		queued: 'Queued',
		running: 'Downloading',
		done: 'Done',
		failed: 'Finished with problems',
		cancelled: 'Cancelled'
	};

	async function cancel() {
		cancelling = true;
		try {
			await api.cancelJob(job.id);
		} finally {
			cancelling = false;
		}
	}
</script>

<article class="card">
	<div class="head">
		<div class="title">
			<strong>{job.title ?? job.url}</strong>
			<span class="muted small">{job.directory ?? job.out_dir}</span>
		</div>
		<span class="state {job.state}">{stateLabel[job.state]}</span>
		{#if active}
			<button onclick={cancel} disabled={cancelling}>Cancel</button>
		{/if}
	</div>

	{#if job.total !== null}
		<div class="bar" role="progressbar" aria-valuenow={job.processed} aria-valuemax={job.total}>
			<div style:width="{percent}%"></div>
		</div>
		<p class="small">
			{job.processed} / {job.total} tracks
			{#each Object.entries(job.counts) as [status, n] (status)}
				<span class="count {status.replace(' ', '-')}">· {n} {status}</span>
			{/each}
		</p>
	{:else if job.state === 'running'}
		<p class="small muted">Reading playlist…</p>
	{/if}

	{#if job.state === 'running'}
		{#if current}
			<p class="small">
				<span class="muted">#{current.index}</span>
				{current.title}
				{#if trackPercent !== null}
					<span class="muted">
						— {trackPercent.toFixed(0)}% of {bytes(current.total_bytes)}{#if current.speed}, {bytes(current.speed)}/s{/if}
					</span>
				{/if}
			</p>
		{:else if waitLeft > 0}
			<p class="small muted">Pausing {seconds(waitLeft)} before the next track</p>
		{/if}
	{/if}

	{#if job.error}<p class="small error">{job.error}</p>{/if}

	{#if job.tracks.length || job.messages.length}
		<details>
			<summary class="small">Details</summary>
			<ol class="tracks small">
				{#each job.tracks as t (t.index)}
					<li class={t.status.replace(' ', '-')} value={t.index}>
						<span class="status">{t.status}</span>
						{t.file ?? t.title}
						{#if t.error}<span class="muted">— {t.error}</span>{/if}
					</li>
				{/each}
			</ol>
			{#if job.messages.length}
				<ul class="messages small muted">
					{#each job.messages as m, i (i)}<li>{m}</li>{/each}
				</ul>
			{/if}
		</details>
	{/if}
</article>

<style>
	.head {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
	.title {
		display: grid;
		min-width: 0;
		flex: 1;
	}
	.title > * {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.small {
		font-size: 0.85rem;
	}
	p {
		margin: 0.5rem 0 0;
	}
	.state {
		font-size: 0.8rem;
		font-weight: 600;
		white-space: nowrap;
		color: var(--muted);
	}
	.state.running {
		color: var(--accent);
	}
	.state.done {
		color: var(--ok);
	}
	.state.failed {
		color: var(--bad);
	}
	.bar {
		height: 6px;
		margin-top: 0.8rem;
		border-radius: 3px;
		background: var(--track);
		overflow: hidden;
	}
	.bar > div {
		height: 100%;
		background: var(--accent);
		transition: width 0.3s;
	}
	.count.failed,
	li.failed .status {
		color: var(--bad);
	}
	.count.unavailable,
	.count.removed,
	li.unavailable .status {
		color: var(--warn);
	}
	li.downloaded .status,
	li.already-exists .status {
		color: var(--ok);
	}
	details {
		margin-top: 0.6rem;
	}
	summary {
		cursor: pointer;
		color: var(--muted);
	}
	.tracks {
		max-height: 20rem;
		overflow: auto;
		margin: 0.5rem 0 0;
		padding-left: 2.5rem;
	}
	.status {
		display: inline-block;
		min-width: 7.5rem;
	}
	.messages {
		margin: 0.5rem 0 0;
		padding-left: 1.2rem;
	}
</style>

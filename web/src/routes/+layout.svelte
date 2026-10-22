<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { live, settings } from '#lib/state.svelte.ts';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	onMount(() => {
		settings.load();
		live.connect();
	});

	const links = [
		{ href: '/', label: 'Download' },
		{ href: '/favourites/', label: 'Favourites' },
		{ href: '/settings/', label: 'Settings' }
	];
	const missingCookies = $derived(settings.value?.cookies.present === false);
</script>

<svelte:head>
	<title>ltube</title>
</svelte:head>

<header>
	<span class="brand">ltube</span>
	<nav>
		{#each links as link (link.href)}
			<a href={link.href} aria-current={page.url.pathname === link.href ? 'page' : undefined}>
				{link.label}
				{#if link.href === '/settings/' && missingCookies}<span class="dot" title="cookies.txt missing"></span>{/if}
			</a>
		{/each}
	</nav>
	{#if !live.connected}<span class="offline">reconnecting…</span>{/if}
</header>

<main>
	{#if settings.error}
		<p class="banner error">Can't reach the ltube server: {settings.error}</p>
	{/if}
	{@render children()}
</main>

<style>
	:global {
		:root {
			--bg: #f6f6f4;
			--surface: #ffffff;
			--border: #e2e2de;
			--text: #1d1d1b;
			--muted: #6b6b66;
			--accent: #c2410c;
			--accent-text: #ffffff;
			--ok: #15803d;
			--warn: #b45309;
			--bad: #b91c1c;
			--track: #ecece8;
			color-scheme: light dark;
		}
		@media (prefers-color-scheme: dark) {
			:root {
				--bg: #141413;
				--surface: #1e1e1c;
				--border: #33332f;
				--text: #ececea;
				--muted: #9a9a94;
				--accent: #f97316;
				--accent-text: #1a0d05;
				--ok: #4ade80;
				--warn: #fbbf24;
				--bad: #f87171;
				--track: #2a2a27;
			}
		}
		* {
			box-sizing: border-box;
		}
		body {
			margin: 0;
			background: var(--bg);
			color: var(--text);
			font: 15px/1.5 system-ui, -apple-system, 'Segoe UI', sans-serif;
		}
		h1 {
			font-size: 1.4rem;
			margin: 0 0 1rem;
		}
		h2 {
			font-size: 1.05rem;
			margin: 2rem 0 0.75rem;
		}
		input[type='text'],
		input[type='url'],
		input[type='number'] {
			width: 100%;
			padding: 0.55rem 0.7rem;
			border: 1px solid var(--border);
			border-radius: 8px;
			background: var(--surface);
			color: var(--text);
			font: inherit;
		}
		input:focus-visible,
		button:focus-visible,
		a:focus-visible {
			outline: 2px solid var(--accent);
			outline-offset: 1px;
		}
		button {
			padding: 0.5rem 1rem;
			border: 1px solid var(--border);
			border-radius: 8px;
			background: var(--surface);
			color: var(--text);
			font: inherit;
			cursor: pointer;
		}
		button.primary {
			background: var(--accent);
			border-color: var(--accent);
			color: var(--accent-text);
			font-weight: 600;
		}
		button:disabled {
			opacity: 0.5;
			cursor: not-allowed;
		}
		.card {
			background: var(--surface);
			border: 1px solid var(--border);
			border-radius: 12px;
			padding: 1.1rem 1.2rem;
		}
		.muted {
			color: var(--muted);
		}
		.banner {
			padding: 0.6rem 0.9rem;
			border-radius: 8px;
			border: 1px solid var(--border);
			background: var(--surface);
		}
		.error {
			color: var(--bad);
		}
		code {
			font-size: 0.9em;
		}
	}

	header {
		display: flex;
		align-items: center;
		gap: 1.5rem;
		max-width: 760px;
		margin: 0 auto;
		padding: 1rem 16px;
	}
	.brand {
		font-weight: 700;
		letter-spacing: -0.02em;
	}
	nav {
		display: flex;
		gap: 0.25rem;
	}
	nav a {
		position: relative;
		padding: 0.3rem 0.7rem;
		border-radius: 6px;
		color: var(--muted);
		text-decoration: none;
	}
	nav a[aria-current='page'] {
		color: var(--text);
		background: var(--surface);
		box-shadow: 0 0 0 1px var(--border);
	}
	.dot {
		display: inline-block;
		width: 7px;
		height: 7px;
		margin-left: 4px;
		border-radius: 50%;
		background: var(--warn);
		vertical-align: middle;
	}
	.offline {
		margin-left: auto;
		color: var(--warn);
		font-size: 0.85rem;
	}
	main {
		max-width: 760px;
		margin: 0 auto;
		padding: 0.5rem 16px 4rem;
	}
</style>

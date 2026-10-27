<script lang="ts">
	import { EXAMPLE, NAME_FIELDS, renderName, trackPatternProblem } from './pattern';

	let {
		label,
		value = $bindable(),
		prefix = '',
		example = EXAMPLE
	}: { label: string; value: string; prefix?: string; example?: Record<string, string> } = $props();

	let input: HTMLInputElement;
	const problem = $derived(trackPatternProblem(value));
	const preview = $derived(renderName(value, example));

	/** Insert a placeholder at the cursor (or replace the selection) and keep typing from there. */
	function insert(field: string) {
		const token = `{${field}}`;
		const start = input.selectionStart ?? value.length;
		const end = input.selectionEnd ?? value.length;
		value = value.slice(0, start) + token + value.slice(end);
		requestAnimationFrame(() => {
			input.focus();
			input.setSelectionRange(start + token.length, start + token.length);
		});
	}
</script>

<div class="field">
	<label>
		<span>{label}</span>
		<input bind:this={input} type="text" bind:value spellcheck="false" />
	</label>
	<div class="chips" role="group" aria-label="Insert placeholder">
		{#each NAME_FIELDS as field (field)}
			<button type="button" class="chip" onclick={() => insert(field)}>{field}</button>
		{/each}
	</div>
	{#if problem}
		<small class="error">{problem}</small>
	{:else}
		<small class="muted">e.g. <code>{prefix}{preview}.opus</code></small>
	{/if}
</div>

<style>
	.field {
		display: grid;
		gap: 0.35rem;
	}
	label {
		display: grid;
		gap: 0.3rem;
	}
	label span {
		font-weight: 600;
		font-size: 0.9rem;
	}
	input {
		font-family: ui-monospace, 'SF Mono', Menlo, monospace;
		font-size: 0.9rem;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.chip {
		padding: 0.15rem 0.55rem;
		border-radius: 999px;
		font-size: 0.8rem;
	}
	.chip::before {
		content: '+ ';
		color: var(--muted);
	}
</style>

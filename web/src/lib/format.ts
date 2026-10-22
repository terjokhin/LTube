export function bytes(n: number | null | undefined): string {
	if (!n) return '0 B';
	const units = ['B', 'KB', 'MB', 'GB'];
	let i = 0;
	while (n >= 1024 && i < units.length - 1) {
		n /= 1024;
		i++;
	}
	return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function seconds(s: number): string {
	s = Math.max(0, Math.ceil(s));
	return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

export function datetime(epochSeconds: number): string {
	return new Date(epochSeconds * 1000).toLocaleString();
}

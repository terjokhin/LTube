export type JobState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

export interface Track {
	index: number;
	title: string;
	status: 'downloaded' | 'already exists' | 'unavailable' | 'failed';
	file: string | null;
	error: string | null;
}

export interface CurrentTrack {
	index: number;
	title: string;
	downloaded_bytes?: number;
	total_bytes?: number | null;
	speed?: number | null;
}

export interface Job {
	id: string;
	url: string;
	out_dir: string;
	playlist_id: string | null;
	created_at: number;
	state: JobState;
	title: string | null;
	directory: string | null;
	total: number | null;
	processed: number;
	counts: Partial<Record<Track['status'] | 'moved', number>>;
	current: CurrentTrack | null;
	waiting_until: number | null;
	messages: string[];
	error: string | null;
	tracks: Track[];
}

/** A playlist ltube has downloaded (or been asked to); favourites are the ones flagged. */
export interface Playlist {
	id: string;
	key: string;
	url: string;
	favourite: boolean;
	title: string | null;
	added_at: number;
	last_synced_at: number | null;
	last_state: JobState | null;
	last_counts: Job['counts'];
	last_error: string | null;
}

export const PACING_FIELDS = [
	'sleep_min',
	'sleep_max',
	'request_sleep',
	'retries',
	'backoff_base',
	'backoff_max',
	'max_consecutive_failures',
	'rate_limit'
] as const;
export type PacingField = (typeof PACING_FIELDS)[number];

export interface Settings {
	out_dir: string;
	/** Fixed by the server (Docker volume mount); the UI shows it read-only. */
	out_dir_locked: boolean;
	/** Track file name inside Artist/Album/, see #lib/pattern.ts; must contain {id}. */
	track_pattern: string;
	/** Pacing (see src/ltube/backoff.py); seconds unless noted. */
	sleep_min: number;
	sleep_max: number;
	request_sleep: number;
	retries: number;
	backoff_base: number;
	backoff_max: number;
	max_consecutive_failures: number;
	/** Bandwidth cap like "2M" (bytes/s); empty = unlimited. */
	rate_limit: string;
	pacing_defaults: Pick<Settings, PacingField>;
	cookies: { present: boolean; updated_at: number | null };
}

export type ServerMessage =
	| { type: 'snapshot'; jobs: Job[]; playlists: Playlist[] }
	| { type: 'playlists'; playlists: Playlist[] }
	| { type: 'job'; job: Omit<Job, 'tracks'> }
	| { type: 'track'; job_id: string; track: Track }
	| { type: 'removed'; job_id: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(path, init);
	if (!res.ok) {
		let message = `${res.status} ${res.statusText}`;
		try {
			const body = await res.json();
			if (typeof body.detail === 'string') message = body.detail;
			else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join('; ');
		} catch {
			// not JSON, keep the status line
		}
		throw new Error(message);
	}
	return res.json();
}

/** Mirror of playlist_key in src/ltube/web/playlists.py: what makes two URLs the same playlist. */
export function playlistKey(url: string): string {
	try {
		const parsed = new URL(url.trim());
		return (
			parsed.searchParams.get('list') ?? parsed.pathname.match(/\/browse\/([\w-]+)/)?.[1] ?? url.trim()
		);
	} catch {
		return url.trim();
	}
}

export const api = {
	settings: () => request<Settings>('/api/settings'),
	saveSettings: (update: Partial<Omit<Settings, 'out_dir_locked' | 'cookies' | 'pacing_defaults'>>) =>
		request<Settings>('/api/settings', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(update)
		}),
	uploadCookies: (file: File) =>
		request<Settings & { loaded: number }>('/api/settings/cookies', {
			method: 'PUT',
			headers: { 'Content-Type': 'text/plain' },
			body: file
		}),
	deleteCookies: () => request<Settings>('/api/settings/cookies', { method: 'DELETE' }),
	startJob: (url: string, favourite: boolean) =>
		request<Job>('/api/jobs', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ url, favourite })
		}),
	syncPlaylist: (id: string) => request<Job>(`/api/playlists/${id}/sync`, { method: 'POST' }),
	setFavourite: (id: string, favourite: boolean) =>
		request<Playlist>(`/api/playlists/${id}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ favourite })
		}),
	cancelJob: (id: string) => request<Job>(`/api/jobs/${id}`, { method: 'DELETE' })
};

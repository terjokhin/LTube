import { api, type Job, type Playlist, type ServerMessage, type Settings } from './api';

class SettingsState {
	value = $state<Settings | null>(null);
	error = $state<string | null>(null);

	async load() {
		try {
			this.value = await api.settings();
			this.error = null;
		} catch (e) {
			this.error = (e as Error).message;
		}
	}
}

/** Everything the server streams over /api/events. */
class LiveState {
	jobs = $state<Job[]>([]);
	playlists = $state<Playlist[]>([]);
	connected = $state(false);
	/** Wall clock in seconds, ticking once a second for countdowns. */
	now = $state(Date.now() / 1000);
	#source: EventSource | null = null;

	connect() {
		if (this.#source) return;
		setInterval(() => (this.now = Date.now() / 1000), 1000);
		// EventSource reconnects on its own; every (re)connect starts with a full snapshot.
		const source = new EventSource('/api/events');
		source.onopen = () => (this.connected = true);
		source.onerror = () => (this.connected = false);
		source.onmessage = (e) => this.#apply(JSON.parse(e.data) as ServerMessage);
		this.#source = source;
	}

	#apply(message: ServerMessage) {
		switch (message.type) {
			case 'snapshot':
				this.jobs = message.jobs.toReversed();
				this.playlists = message.playlists;
				break;
			case 'playlists':
				this.playlists = message.playlists;
				break;
			case 'job': {
				const job = this.jobs.find((j) => j.id === message.job.id);
				if (job) Object.assign(job, message.job);
				else this.jobs.unshift({ ...message.job, tracks: [] });
				break;
			}
			case 'track':
				this.jobs.find((j) => j.id === message.job_id)?.tracks.push(message.track);
				break;
			case 'removed':
				this.jobs = this.jobs.filter((j) => j.id !== message.job_id);
				break;
		}
	}
}

export const settings = new SettingsState();
export const live = new LiveState();

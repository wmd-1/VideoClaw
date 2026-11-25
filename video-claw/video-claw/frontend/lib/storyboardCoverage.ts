export interface ScriptEpisodeLike {
  episode_number?: number | string;
  act_number?: number | string;
  act_title?: string;
}

export interface StoryboardEpisodeLike {
  episode_number?: number | string;
  act_number?: number | string;
  episode_title?: string;
  segments?: unknown[];
}

export interface StoryboardCoverage {
  complete: boolean;
  scriptEpisodes: number[];
  storyboardEpisodes: number[];
  missingEpisodes: number[];
  emptyEpisodes: number[];
}

function episodeNumber(episode: { episode_number?: unknown; act_number?: unknown }): number | null {
  const raw = episode.episode_number ?? episode.act_number;
  if (raw === null || raw === undefined || !/^\d+$/.test(String(raw))) return null;
  return Number(raw);
}

/** 与后端 script_revision.storyboard_coverage 保持同一业务语义。 */
export function getStoryboardCoverage(
  scriptEpisodes: ScriptEpisodeLike[] | undefined,
  storyboardEpisodes: StoryboardEpisodeLike[] | undefined,
): StoryboardCoverage {
  const scriptNumbers = Array.from(new Set(
    (scriptEpisodes || [])
      .map(episodeNumber)
      .filter((value): value is number => value !== null),
  )).sort((a, b) => a - b);
  const storyboardByNumber = new Map<number, StoryboardEpisodeLike>();
  for (const episode of storyboardEpisodes || []) {
    const number = episodeNumber(episode);
    if (number !== null) storyboardByNumber.set(number, episode);
  }
  const storyboardNumbers = Array.from(storyboardByNumber.keys()).sort((a, b) => a - b);
  const missingEpisodes = scriptNumbers.filter(number => !storyboardByNumber.has(number));
  const emptyEpisodes = scriptNumbers.filter(number => {
    const episode = storyboardByNumber.get(number);
    if (!episode) return false;
    return !Array.isArray(episode.segments) || episode.segments.length === 0;
  });
  return {
    complete: scriptNumbers.length > 0 && missingEpisodes.length === 0 && emptyEpisodes.length === 0,
    scriptEpisodes: scriptNumbers,
    storyboardEpisodes: storyboardNumbers,
    missingEpisodes,
    emptyEpisodes,
  };
}

export function isStoryboardConfirmable(
  scriptEpisodes: ScriptEpisodeLike[] | undefined,
  storyboardEpisodes: StoryboardEpisodeLike[] | undefined,
): boolean {
  return getStoryboardCoverage(scriptEpisodes, storyboardEpisodes).complete;
}

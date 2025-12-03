import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import api from '../api';

const useSettingsStore = create(
  persist(
    (set, get) => ({
      settings: {},
      environment: {
        // Add default values for environment settings
        public_ip: '',
        country_code: '',
        country_name: '',
        env_mode: 'prod',
      },
      isLoading: false,
      error: null,

      // Stream format preference: 'ts' (MPEG-TS) or 'hls' (HLS)
      streamFormat: 'ts',

      // M3U URL parameters (cached logos, direct, tvg_id_source)
      m3uParams: {
        cachedlogos: true,
        direct: false,
        tvg_id_source: 'channel_number',
      },

      fetchSettings: async () => {
        set({ isLoading: true, error: null });
        try {
          const settings = await api.getSettings();
          const env = await api.getEnvironmentSettings();
          set({
            settings: settings.reduce((acc, setting) => {
              acc[setting.key] = setting;
              return acc;
            }, {}),
            isLoading: false,
            environment: env || {
              public_ip: '',
              country_code: '',
              country_name: '',
              env_mode: 'prod',
            },
          });
        } catch (error) {
          set({ error: 'Failed to load settings.', isLoading: false });
        }
      },

      updateSetting: (setting) =>
        set((state) => ({
          settings: { ...state.settings, [setting.key]: setting },
        })),

      // Set stream output format ('ts' or 'hls')
      setStreamFormat: (format) => set({ streamFormat: format }),

      // Update M3U parameters
      setM3uParams: (params) =>
        set((state) => ({
          m3uParams: { ...state.m3uParams, ...params },
        })),

      // Get the appropriate channel URL based on format
      getChannelUrl: (channelUuid, baseUrl) => {
        const { streamFormat } = get();
        if (streamFormat === 'hls') {
          // Use playlist.m3u8 which is the master playlist entry point
          // This redirects to index.m3u8 (media playlist) internally
          return `${baseUrl}/output/hls/${channelUuid}/playlist.m3u8`;
        }
        return `${baseUrl}/proxy/ts/stream/${channelUuid}`;
      },
    }),
    {
      name: 'dispatcharr-settings',
      // Only persist the user preferences, not the fetched settings
      partialize: (state) => ({
        streamFormat: state.streamFormat,
        m3uParams: state.m3uParams,
      }),
    }
  )
);

export default useSettingsStore;

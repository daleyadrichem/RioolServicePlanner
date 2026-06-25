import { useCallback, useEffect, useState } from 'react';

export function useApi(loadFn, fallbackValue) {
  const [data, setData] = useState(fallbackValue);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const reload = useCallback(async (options = {}) => {
    const silent = Boolean(options.silent);
    try {
      if (!silent) setLoading(true);
      setError('');
      setData(await loadFn());
    } catch (err) {
      setError('Backend niet bereikbaar. Fallback mockdata wordt getoond.');
      setData(fallbackValue);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [loadFn, fallbackValue]);

  useEffect(() => { reload(); }, [reload]);

  return { data, setData, loading, error, reload };
}

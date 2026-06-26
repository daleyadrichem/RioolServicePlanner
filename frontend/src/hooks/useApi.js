import { useCallback, useEffect, useState } from 'react';

export function useApi(loadFn, initialValue) {
  const [data, setData] = useState(initialValue);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const reload = useCallback(async (options = {}) => {
    const silent = Boolean(options.silent);
    try {
      if (!silent) setLoading(true);
      setError('');
      setData(await loadFn());
    } catch (err) {
      setError('Backend niet bereikbaar. Bestaande data blijft zichtbaar; er wordt opnieuw geprobeerd bij de volgende refresh.');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [loadFn]);

  useEffect(() => { reload(); }, [reload]);

  return { data, setData, loading, error, reload };
}

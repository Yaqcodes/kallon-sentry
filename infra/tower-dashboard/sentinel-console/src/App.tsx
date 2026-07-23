import { useState } from 'react';
import SentinelConsole from './components/SentinelConsole';
import LocalRecordingsView from './components/LocalRecordingsView';

type View = 'live' | 'recordings';

export default function App() {
  const [view, setView] = useState<View>('live');

  if (view === 'recordings') {
    return <LocalRecordingsView onBackToLive={() => setView('live')} />;
  }

  return <SentinelConsole onOpenRecordings={() => setView('recordings')} />;
}

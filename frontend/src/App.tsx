import { useState } from 'react'
import Chat from './views/Chat'
import QuestList from './views/QuestList'
import Review from './views/Review'

type View =
  | { name: 'list' }
  | { name: 'chat'; questId: string }
  | { name: 'review'; questId: string }

export default function App() {
  const [view, setView] = useState<View>({ name: 'list' })

  switch (view.name) {
    case 'chat':
      return (
        <Chat
          questId={view.questId}
          onBack={() => setView({ name: 'list' })}
          onGotoReview={(id) => setView({ name: 'review', questId: id })}
        />
      )
    case 'review':
      return (
        <Review questId={view.questId} onBack={() => setView({ name: 'list' })} />
      )
    default:
      return <QuestList onOpen={(id) => setView({ name: 'chat', questId: id })} />
  }
}

import { SectionBoundary } from '../layout/SectionBoundary'
import { HeroStrip } from './HeroStrip'
import { ForgeSystemCard, PaperSystemCard, LiveSystemCard } from './SystemCards'
import { GatesSummary } from './GatesSummary'
import { ActivityFeed } from './ActivityFeed'
import type { TabId } from '../layout/TabBar'

/** Command — the overview landing: one glance across forge / paper book / live. */
export function CommandTab({ onNavigate }: { onNavigate: (tab: TabId) => void }) {
  return (
    <div className="stagger-pop space-y-4" data-section="command">
      <SectionBoundary title="hero">
        <HeroStrip />
      </SectionBoundary>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <SectionBoundary title="forge-card">
          <ForgeSystemCard onNavigate={onNavigate} />
        </SectionBoundary>
        <SectionBoundary title="paper-card">
          <PaperSystemCard onNavigate={onNavigate} />
        </SectionBoundary>
        <SectionBoundary title="live-card">
          <LiveSystemCard onNavigate={onNavigate} />
        </SectionBoundary>
      </div>

      <SectionBoundary title="gates">
        <GatesSummary />
      </SectionBoundary>

      <SectionBoundary title="activity">
        <ActivityFeed />
      </SectionBoundary>
    </div>
  )
}

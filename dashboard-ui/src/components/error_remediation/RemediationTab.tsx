import { SectionBoundary } from '../layout/SectionBoundary'
import { RemediationPanel } from './RemediationPanel'

export function RemediationTab() {
  return (
    <SectionBoundary title="Error Remediation">
      <RemediationPanel />
    </SectionBoundary>
  )
}

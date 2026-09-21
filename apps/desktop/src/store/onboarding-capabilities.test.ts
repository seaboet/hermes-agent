import { afterEach, expect, it, vi } from 'vitest'

import { readOnboardingCapabilities } from '@/store/onboarding-capabilities'
import { buildChatOnboardingSeedMessages } from '@/store/onboarding-script'

const requestGatewayForAgent = vi.hoisted(() => vi.fn())

// The gateway module is the required RPC seam; this test pins the exact connection/profile request without opening a backend.
// oxlint-disable-next-line anti-slop/no-module-mocking
vi.mock('@/store/gateway', () => ({ requestGatewayForAgent }))

it('carries fresh catalog evidence into the guide, read from the pinned backend', async () => {
  const entry = {
    name: 'future-studio', description: 'Future Studio integration', source: 'https://example.test',
    transport: 'stdio', auth_type: 'none', requires: [], required_env: [], command: 'example', args: [],
    url: null, install_url: null, install_ref: null, bootstrap: [], default_enabled: null,
    post_install: 'Enable the app integration.', needs_install: false, installed: false, enabled: false,
    requires_app: true,
    availability: { state: 'available', version: null, path: null, min_version: null },
    suggest: {
      keywords: ['future studio'], hosts: [], applications: ['Future Studio'],
      examples: ['Make a scene in Future Studio']
    }
  }

  requestGatewayForAgent.mockResolvedValue({ servers: [entry], diagnostics: [] })
  const scope = { connectionId: 'remote-studio', profile: 'default' }
  const evidence = await readOnboardingCapabilities(scope)
  const guide = buildChatOnboardingSeedMessages('Hi', true, evidence)
  expect(guide[0].content).toContain('"name":"future-studio"')
  expect(guide[0].content).toContain('"readiness":"setup_required"')
  expect(guide[0].display_kind).toBe('hidden')
  expect(requestGatewayForAgent).toHaveBeenLastCalledWith(
    scope.connectionId,
    scope.profile,
    'mcp.catalog',
    {},
    undefined,
    undefined,
    { spawnPriority: 'background' }
  )
})

afterEach(() => {
  requestGatewayForAgent.mockReset()
})

it('reads only the pinned backend and degrades safely when the RPC rejects', async () => {
  const scope = { connectionId: 'remote-studio', profile: 'hermes-setup' }
  requestGatewayForAgent.mockResolvedValueOnce({ servers: [], diagnostics: [] })
  expect(await readOnboardingCapabilities(scope)).toBe('')
  expect(requestGatewayForAgent).toHaveBeenCalledWith(
    scope.connectionId,
    scope.profile,
    'mcp.catalog',
    {},
    undefined,
    undefined,
    { spawnPriority: 'background' }
  )

  requestGatewayForAgent.mockRejectedValueOnce(new Error('Offline'))
  expect(await readOnboardingCapabilities(scope)).toBe('')
  expect(requestGatewayForAgent).toHaveBeenCalledTimes(2)
})

<template>
  <div class="workbench-panel">
    <div class="scroll-container">
      <!-- Step 01: Ontology -->
      <div class="step-card" :class="{ 'active': currentPhase === 0, 'completed': currentPhase > 0 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">01</span>
            <span class="step-title">{{ $t('step1.ontologyGeneration') }}</span>
          </div>
          <div class="step-status">
            <span v-if="currentPhase > 0" class="badge success">{{ $t('step1.ontologyCompleted') }}</span>
            <span v-else-if="currentPhase === 0" class="badge processing">{{ $t('step1.ontologyGenerating') }}</span>
            <span v-else class="badge pending">{{ $t('step1.ontologyPending') }}</span>
          </div>
        </div>
        
        <div class="card-content">
          <p class="api-note">POST /api/graph/ontology/generate</p>
          <p class="description">
            {{ $t('step1.ontologyDesc') }}
          </p>

          <!-- Loading / Progress -->
          <div v-if="currentPhase === 0 && ontologyProgress" class="progress-section">
            <div class="spinner-sm"></div>
            <span>{{ ontologyProgress.message || $t('step1.analyzingDocs') }}</span>
          </div>

          <!-- Detail Overlay -->
          <div v-if="selectedOntologyItem" class="ontology-detail-overlay">
            <div class="detail-header">
               <div class="detail-title-group">
                  <span class="detail-type-badge">{{ selectedOntologyItem.itemType === 'entity' ? 'ENTITY' : 'RELATION' }}</span>
                  <span class="detail-name">{{ selectedOntologyItem.name }}</span>
               </div>
               <button class="close-btn" @click="selectedOntologyItem = null">×</button>
            </div>
            <div class="detail-body">
               <div class="detail-desc">{{ selectedOntologyItem.description }}</div>
               
               <!-- Attributes -->
               <div class="detail-section" v-if="selectedOntologyItem.attributes?.length">
                  <span class="section-label">ATTRIBUTES</span>
                  <div class="attr-list">
                     <div v-for="attr in selectedOntologyItem.attributes" :key="attr.name" class="attr-item">
                        <span class="attr-name">{{ attr.name }}</span>
                        <span class="attr-type">({{ attr.type }})</span>
                        <span class="attr-desc">{{ attr.description }}</span>
                     </div>
                  </div>
               </div>

               <!-- Examples (Entity) -->
               <div class="detail-section" v-if="selectedOntologyItem.examples?.length">
                  <span class="section-label">EXAMPLES</span>
                  <div class="example-list">
                     <span v-for="ex in selectedOntologyItem.examples" :key="ex" class="example-tag">{{ ex }}</span>
                  </div>
               </div>

               <!-- Source/Target (Relation) -->
               <div class="detail-section" v-if="selectedOntologyItem.source_targets?.length">
                  <span class="section-label">CONNECTIONS</span>
                  <div class="conn-list">
                     <div v-for="(conn, idx) in selectedOntologyItem.source_targets" :key="idx" class="conn-item">
                        <span class="conn-node">{{ conn.source }}</span>
                        <span class="conn-arrow">→</span>
                        <span class="conn-node">{{ conn.target }}</span>
                     </div>
                  </div>
               </div>
            </div>
          </div>

          <!-- Generated Entity Tags -->
          <div v-if="projectData?.ontology?.entity_types" class="tags-container" :class="{ 'dimmed': selectedOntologyItem }">
            <span class="tag-label">GENERATED ENTITY TYPES</span>
            <div class="tags-list">
              <span 
                v-for="entity in projectData.ontology.entity_types" 
                :key="entity.name" 
                class="entity-tag clickable"
                @click="selectOntologyItem(entity, 'entity')"
              >
                {{ entity.name }}
              </span>
            </div>
          </div>

          <!-- Generated Relation Tags -->
          <div v-if="projectData?.ontology?.edge_types" class="tags-container" :class="{ 'dimmed': selectedOntologyItem }">
            <span class="tag-label">GENERATED RELATION TYPES</span>
            <div class="tags-list">
              <span 
                v-for="rel in projectData.ontology.edge_types" 
                :key="rel.name" 
                class="entity-tag clickable"
                @click="selectOntologyItem(rel, 'relation')"
              >
                {{ rel.name }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 02: Graph Build -->
      <div class="step-card" :class="{ 'active': currentPhase === 1, 'completed': currentPhase > 1 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">02</span>
            <span class="step-title">{{ $t('step1.graphRagBuild') }}</span>
          </div>
          <div class="step-status">
            <span v-if="currentPhase > 1" class="badge success">{{ $t('step1.ontologyCompleted') }}</span>
            <span v-else-if="currentPhase === 1" class="badge processing">{{ buildProgress?.progress || 0 }}%</span>
            <span v-else class="badge pending">{{ $t('step1.ontologyPending') }}</span>
          </div>
        </div>

        <div class="card-content">
          <p class="api-note">POST /api/graph/build</p>
          <p class="description">
            {{ $t('step1.graphRagDesc') }}
          </p>
          
          <!-- Stats Cards -->
          <div class="stats-grid">
            <div class="stat-card">
              <span class="stat-value">{{ graphStats.nodes }}</span>
              <span class="stat-label">{{ $t('step1.entityNodes') }}</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ graphStats.edges }}</span>
              <span class="stat-label">{{ $t('step1.relationEdges') }}</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ graphStats.types }}</span>
              <span class="stat-label">{{ $t('step1.schemaTypes') }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 03: Complete -->
      <div class="step-card" :class="{ 'active': currentPhase === 2, 'completed': currentPhase >= 2 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">03</span>
            <span class="step-title">{{ $t('step1.buildComplete') }}</span>
          </div>
          <div class="step-status">
            <span v-if="currentPhase >= 2" class="badge accent">{{ $t('step1.inProgress') }}</span>
          </div>
        </div>

        <div class="card-content">
          <p class="api-note">POST /api/simulation/create</p>
          <p class="description">{{ $t('step1.buildCompleteDesc') }}</p>
          <button
            class="action-btn"
            :disabled="currentPhase < 2 || creatingSimulation"
            @click="handleEnterEnvSetup"
          >
            <span v-if="creatingSimulation" class="spinner-sm"></span>
            {{ creatingSimulation ? $t('step1.creating') : $t('step1.enterEnvSetup') + ' ➝' }}
          </button>
        </div>
      </div>

      <!-- Project hub: requirement edit + simulations list. Visible once
           the project is loaded (graph_completed or before). Lets users
           run multiple scenarios on the same KG without rewriting the
           project's requirement. -->
      <div class="step-card" v-if="projectData?.project_id">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">★</span>
            <span class="step-title">{{ $t('project.requirementLabel') }}</span>
          </div>
          <div class="step-status">
            <button
              v-if="!editingRequirement"
              class="badge ghost"
              @click="startEditRequirement"
            >{{ $t('project.editRequirement') }}</button>
          </div>
        </div>
        <div class="card-content">
          <p
            v-if="!editingRequirement"
            class="description requirement-display"
          >{{ projectData.simulation_requirement || '-' }}</p>
          <div v-else class="hub-edit-form">
            <textarea
              v-model="requirementDraft"
              rows="6"
              class="hub-textarea"
              :placeholder="$t('project.requirementPlaceholder')"
            ></textarea>
            <div class="hub-actions">
              <button class="btn-secondary" @click="cancelEditRequirement">{{ $t('common.cancel') }}</button>
              <button class="btn-primary" :disabled="savingRequirement" @click="saveRequirement">
                {{ savingRequirement ? $t('common.saving') : $t('common.save') }}
              </button>
            </div>
          </div>
        </div>

        <div class="card-content sims-section">
          <div class="sims-header">
            <span class="sims-title">{{ $t('project.simulationsTitle') }}</span>
            <button class="badge ghost" @click="openNewSimModal" :disabled="!projectData.graph_id">
              + {{ $t('project.newSimulation') }}
            </button>
          </div>
          <div v-if="projectSims.length" class="sims-list">
            <div
              v-for="sim in projectSims"
              :key="sim.simulation_id"
              class="sim-row"
              @click="openSim(sim.simulation_id)"
            >
              <div class="sim-row-top">
                <span class="sim-id">{{ sim.simulation_id }}</span>
                <span class="sim-status" :class="`status-${sim.status}`">{{ sim.status }}</span>
              </div>
              <div class="sim-row-meta">
                <span v-if="sim.profiles_count">{{ sim.profiles_count }} agents</span>
                <span v-if="sim.simulation_requirement">
                  · {{ truncateText(sim.simulation_requirement, 60) }}
                </span>
              </div>
            </div>
          </div>
          <div v-else class="sims-empty">{{ $t('project.simulationsEmpty') }}</div>
        </div>
      </div>
    </div>

    <!-- New simulation modal -->
    <div v-if="showNewSimModal" class="hub-modal-backdrop" @click.self="closeNewSimModal">
      <div class="hub-modal-card">
        <h3 class="hub-modal-title">{{ $t('project.newSimulationTitle') }}</h3>
        <p class="hub-modal-help">{{ $t('project.newSimulationHelp') }}</p>
        <textarea
          v-model="newSimRequirement"
          rows="8"
          class="hub-textarea"
          :placeholder="$t('project.requirementPlaceholder')"
        ></textarea>
        <div class="hub-modal-options">
          <label><input type="checkbox" v-model="newSimEnableReddit"> Reddit</label>
          <label><input type="checkbox" v-model="newSimEnableTwitter"> Twitter</label>
        </div>
        <div class="hub-actions">
          <button class="btn-secondary" @click="closeNewSimModal">{{ $t('common.cancel') }}</button>
          <button class="btn-primary" :disabled="creatingNewSim" @click="confirmCreateSim">
            {{ creatingNewSim ? $t('common.saving') : $t('project.createSimulation') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Bottom Info / Logs -->
    <div class="system-logs">
      <div class="log-header">
        <span class="log-title">SYSTEM DASHBOARD</span>
        <span class="log-id">{{ projectData?.project_id || 'NO_PROJECT' }}</span>
      </div>
      <div class="log-content" ref="logContent">
        <div class="log-line" v-for="(log, idx) in systemLogs" :key="idx">
          <span class="log-time">{{ log.time }}</span>
          <span class="log-msg">{{ log.msg }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { createSimulation } from '../api/simulation'
import { updateProject, listProjectSimulations, createSimulation as createSimulationGraph } from '../api/graph'

const router = useRouter()
const { t } = useI18n()

const props = defineProps({
  currentPhase: { type: Number, default: 0 },
  projectData: Object,
  ontologyProgress: Object,
  buildProgress: Object,
  graphData: Object,
  systemLogs: { type: Array, default: () => [] }
})

defineEmits(['next-step'])

const selectedOntologyItem = ref(null)
const logContent = ref(null)
const creatingSimulation = ref(false)

// Project hub state
const editingRequirement = ref(false)
const requirementDraft = ref('')
const savingRequirement = ref(false)
const projectSims = ref([])
const showNewSimModal = ref(false)
const newSimRequirement = ref('')
const newSimEnableReddit = ref(true)
const newSimEnableTwitter = ref(true)
const creatingNewSim = ref(false)

const truncateText = (s, n) => {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + '…' : s
}

const startEditRequirement = () => {
  requirementDraft.value = props.projectData?.simulation_requirement || ''
  editingRequirement.value = true
}

const cancelEditRequirement = () => {
  editingRequirement.value = false
  requirementDraft.value = ''
}

const saveRequirement = async () => {
  const pid = props.projectData?.project_id
  if (!pid) return
  savingRequirement.value = true
  try {
    const res = await updateProject(pid, {
      simulation_requirement: requirementDraft.value,
    })
    if (res?.success && res.data) {
      // Mutate the prop so the display updates immediately. The parent
      // also holds projectData, so this stays in sync until the next
      // getProject() refresh.
      Object.assign(props.projectData, res.data)
    }
    editingRequirement.value = false
  } catch (e) {
    console.error('updateProject failed', e)
    alert(e?.response?.data?.error || e.message || 'update failed')
  } finally {
    savingRequirement.value = false
  }
}

const loadProjectSimulations = async () => {
  const pid = props.projectData?.project_id
  if (!pid) return
  try {
    const res = await listProjectSimulations(pid)
    if (res?.success) projectSims.value = res.data || []
  } catch (e) {
    console.warn('listProjectSimulations failed (non-fatal)', e)
  }
}

watch(() => props.projectData?.project_id, (pid) => {
  if (pid) loadProjectSimulations()
}, { immediate: true })

const openSim = (simId) => {
  router.push({ name: 'Simulation', params: { simulationId: simId } })
}

const openNewSimModal = () => {
  newSimRequirement.value = props.projectData?.simulation_requirement || ''
  newSimEnableReddit.value = true
  newSimEnableTwitter.value = true
  showNewSimModal.value = true
}

const closeNewSimModal = () => {
  showNewSimModal.value = false
}

const confirmCreateSim = async () => {
  const pid = props.projectData?.project_id
  const gid = props.projectData?.graph_id
  if (!pid || !gid) return
  creatingNewSim.value = true
  try {
    const projectReq = (props.projectData.simulation_requirement || '').trim()
    const draft = (newSimRequirement.value || '').trim()
    const payload = {
      project_id: pid,
      graph_id: gid,
      enable_reddit: newSimEnableReddit.value,
      enable_twitter: newSimEnableTwitter.value,
    }
    // Only send override when it differs from the project default
    if (draft && draft !== projectReq) {
      payload.simulation_requirement = draft
    }
    const res = await createSimulationGraph(payload)
    if (res?.success && res.data?.simulation_id) {
      showNewSimModal.value = false
      router.push({ name: 'Simulation', params: { simulationId: res.data.simulation_id } })
    }
  } catch (e) {
    console.error('createSimulation failed', e)
    alert(e?.response?.data?.error || e.message || 'create failed')
  } finally {
    creatingNewSim.value = false
  }
}

// 进入环境搭建 - 创建 simulation 并跳转
const handleEnterEnvSetup = async () => {
  if (!props.projectData?.project_id || !props.projectData?.graph_id) {
    console.error('缺少项目或图谱信息')
    return
  }
  
  creatingSimulation.value = true
  
  try {
    const res = await createSimulation({
      project_id: props.projectData.project_id,
      graph_id: props.projectData.graph_id,
      enable_twitter: true,
      enable_reddit: true
    })
    
    if (res.success && res.data?.simulation_id) {
      // 跳转到 simulation 页面
      router.push({
        name: 'Simulation',
        params: { simulationId: res.data.simulation_id }
      })
    } else {
      console.error('创建模拟失败:', res.error)
      alert(t('step1.createSimulationFailed', { error: res.error || t('common.unknownError') }))
    }
  } catch (err) {
    console.error('创建模拟异常:', err)
    alert(t('step1.createSimulationException', { error: err.message }))
  } finally {
    creatingSimulation.value = false
  }
}

const selectOntologyItem = (item, type) => {
  selectedOntologyItem.value = { ...item, itemType: type }
}

const graphStats = computed(() => {
  const nodes = props.graphData?.node_count || props.graphData?.nodes?.length || 0
  const edges = props.graphData?.edge_count || props.graphData?.edges?.length || 0
  const types = props.projectData?.ontology?.entity_types?.length || 0
  return { nodes, edges, types }
})

const formatDate = (dateStr) => {
  if (!dateStr) return '--:--:--'
  const d = new Date(dateStr)
  return d.toLocaleTimeString('en-US', { hour12: false }) + '.' + d.getMilliseconds()
}

// Auto-scroll logs
watch(() => props.systemLogs.length, () => {
  nextTick(() => {
    if (logContent.value) {
      logContent.value.scrollTop = logContent.value.scrollHeight
    }
  })
})
</script>

<style scoped>
.workbench-panel {
  height: 100%;
  background-color: #FAFAFA;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

.scroll-container {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.step-card {
  background: #FFF;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
  border: 1px solid #EAEAEA;
  transition: all 0.3s ease;
  position: relative; /* For absolute overlay */
}

.step-card.active {
  border-color: #FF5722;
  box-shadow: 0 4px 12px rgba(255, 87, 34, 0.08);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.step-info {
  display: flex;
  align-items: center;
  gap: 12px;
}

.step-num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 20px;
  font-weight: 700;
  color: #E0E0E0;
}

.step-card.active .step-num,
.step-card.completed .step-num {
  color: #000;
}

.step-title {
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.5px;
}

.badge {
  font-size: 10px;
  padding: 4px 8px;
  border-radius: 4px;
  font-weight: 600;
  text-transform: uppercase;
}

.badge.success { background: #E8F5E9; color: #2E7D32; }
.badge.processing { background: #FF5722; color: #FFF; }
.badge.accent { background: #FF5722; color: #FFF; }
.badge.pending { background: #F5F5F5; color: #999; }

.api-note {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #999;
  margin-bottom: 8px;
}

.description {
  font-size: 12px;
  color: #666;
  line-height: 1.5;
  margin-bottom: 16px;
}

/* Step 01 Tags */
.tags-container {
  margin-top: 12px;
  transition: opacity 0.3s;
}

.tags-container.dimmed {
    opacity: 0.3;
    pointer-events: none;
}

.tag-label {
  display: block;
  font-size: 10px;
  color: #AAA;
  margin-bottom: 8px;
  font-weight: 600;
}

.tags-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.entity-tag {
  background: #F5F5F5;
  border: 1px solid #EEE;
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 11px;
  color: #333;
  font-family: 'JetBrains Mono', monospace;
  transition: all 0.2s;
}

.entity-tag.clickable {
    cursor: pointer;
}

.entity-tag.clickable:hover {
    background: #E0E0E0;
    border-color: #CCC;
}

/* Ontology Detail Overlay */
.ontology-detail-overlay {
    position: absolute;
    top: 60px; /* Below header roughly */
    left: 20px;
    right: 20px;
    bottom: 20px;
    background: rgba(255, 255, 255, 0.98);
    backdrop-filter: blur(4px);
    z-index: 10;
    border: 1px solid #EAEAEA;
    box-shadow: 0 4px 20px rgba(0,0,0,0.05);
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    animation: fadeIn 0.2s ease-out;
}

@keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

.detail-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid #EAEAEA;
    background: #FAFAFA;
}

.detail-title-group {
    display: flex;
    align-items: center;
    gap: 8px;
}

.detail-type-badge {
    font-size: 9px;
    font-weight: 700;
    color: #FFF;
    background: #000;
    padding: 2px 6px;
    border-radius: 2px;
    text-transform: uppercase;
}

.detail-name {
    font-size: 14px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
}

.close-btn {
    background: none;
    border: none;
    font-size: 18px;
    color: #999;
    cursor: pointer;
    line-height: 1;
}

.close-btn:hover {
    color: #333;
}

.detail-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
}

.detail-desc {
    font-size: 12px;
    color: #444;
    line-height: 1.5;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px dashed #EAEAEA;
}

.detail-section {
    margin-bottom: 16px;
}

.section-label {
    display: block;
    font-size: 10px;
    font-weight: 600;
    color: #AAA;
    margin-bottom: 8px;
}

.attr-list, .conn-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.attr-item {
    font-size: 11px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: baseline;
    padding: 4px;
    background: #F9F9F9;
    border-radius: 4px;
}

.attr-name {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
    color: #000;
}

.attr-type {
    color: #999;
    font-size: 10px;
}

.attr-desc {
    color: #555;
    flex: 1;
    min-width: 150px;
}

.example-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}

.example-tag {
    font-size: 11px;
    background: #FFF;
    border: 1px solid #E0E0E0;
    padding: 3px 8px;
    border-radius: 12px;
    color: #555;
}

.conn-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    padding: 6px;
    background: #F5F5F5;
    border-radius: 4px;
    font-family: 'JetBrains Mono', monospace;
}

.conn-node {
    font-weight: 600;
    color: #333;
}

.conn-arrow {
    color: #BBB;
}

/* Step 02 Stats */
.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
  background: #F9F9F9;
  padding: 16px;
  border-radius: 6px;
}

.stat-card {
  text-align: center;
}

.stat-value {
  display: block;
  font-size: 20px;
  font-weight: 700;
  color: #000;
  font-family: 'JetBrains Mono', monospace;
}

.stat-label {
  font-size: 9px;
  color: #999;
  text-transform: uppercase;
  margin-top: 4px;
  display: block;
}

/* Step 03 Button */
.action-btn {
  width: 100%;
  background: #000;
  color: #FFF;
  border: none;
  padding: 14px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.2s;
}

.action-btn:hover:not(:disabled) {
  opacity: 0.8;
}

.action-btn:disabled {
  background: #CCC;
  cursor: not-allowed;
}

.progress-section {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #FF5722;
  margin-bottom: 12px;
}

.spinner-sm {
  width: 14px;
  height: 14px;
  border: 2px solid #FFCCBC;
  border-top-color: #FF5722;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* System Logs */
.system-logs {
  background: #000;
  color: #DDD;
  padding: 16px;
  font-family: 'JetBrains Mono', monospace;
  border-top: 1px solid #222;
  flex-shrink: 0;
}

.log-header {
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid #333;
  padding-bottom: 8px;
  margin-bottom: 8px;
  font-size: 10px;
  color: #888;
}

.log-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
  height: 80px; /* Approx 4 lines visible */
  overflow-y: auto;
  padding-right: 4px;
}

.log-content::-webkit-scrollbar {
  width: 4px;
}

.log-content::-webkit-scrollbar-thumb {
  background: #333;
  border-radius: 2px;
}

.log-line {
  font-size: 11px;
  display: flex;
  gap: 12px;
  line-height: 1.5;
}

.log-time {
  color: #666;
  min-width: 75px;
}

.log-msg {
  color: #CCC;
  word-break: break-all;
}

/* ====== Project hub additions ====== */
.requirement-display {
  white-space: pre-wrap;
  background: #FAFAFA;
  border: 1px solid #EAEAEA;
  border-radius: 4px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.5;
  color: #444;
}
.hub-edit-form { display: flex; flex-direction: column; gap: 8px; }
.hub-textarea {
  width: 100%;
  resize: vertical;
  font-family: inherit;
  font-size: 13px;
  padding: 8px 10px;
  border: 1px solid #DDD;
  border-radius: 4px;
  background: #FFF;
  box-sizing: border-box;
}
.hub-actions { display: flex; gap: 8px; justify-content: flex-end; }
.btn-secondary, .btn-primary {
  padding: 6px 14px;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid transparent;
}
.btn-secondary { background: #FFF; border-color: #DDD; color: #333; }
.btn-primary { background: #DAA520; border-color: #DAA520; color: #FFF; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }

.badge.ghost {
  background: transparent;
  border: 1px solid currentColor;
  color: #DAA520;
  cursor: pointer;
  padding: 2px 10px;
  font-size: 11px;
  border-radius: 4px;
}
.badge.ghost:hover { background: #DAA520; color: #FFF; }
.badge.ghost:disabled { opacity: 0.5; cursor: not-allowed; }

.sims-section { border-top: 1px dashed #EAEAEA; padding-top: 12px; }
.sims-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.sims-title {
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.sims-list { display: flex; flex-direction: column; gap: 6px; }
.sim-row {
  border: 1px solid #EAEAEA;
  border-radius: 4px;
  padding: 8px 10px;
  cursor: pointer;
  transition: background 0.1s;
}
.sim-row:hover { background: #FAFAFA; }
.sim-row-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.sim-id { font-family: monospace; font-size: 11px; color: #555; }
.sim-status {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 10px;
  text-transform: uppercase;
}
.sim-status.status-ready, .sim-status.status-completed { background: #E6F4EA; color: #137333; }
.sim-status.status-running, .sim-status.status-preparing { background: #FFF8E1; color: #B8860B; }
.sim-status.status-failed, .sim-status.status-stopped { background: #FCE8E6; color: #C5221F; }
.sim-status.status-created { background: #F1F3F4; color: #5F6368; }
.sim-row-meta {
  font-size: 11px;
  color: #777;
  margin-top: 3px;
}
.sims-empty {
  font-size: 12px;
  color: #999;
  padding: 8px 0;
  text-align: center;
}

.hub-modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.hub-modal-card {
  background: #FFF;
  border-radius: 8px;
  padding: 24px;
  width: 560px;
  max-width: 90vw;
  max-height: 80vh;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.hub-modal-title { margin: 0; font-size: 16px; font-weight: 600; }
.hub-modal-help { font-size: 13px; color: #666; margin: 0; }
.hub-modal-options { display: flex; gap: 16px; font-size: 13px; }
</style>

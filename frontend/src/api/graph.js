import service, { requestWithRetry } from './index'

/**
 * 生成本体（上传文档和模拟需求）
 * @param {Object} data - 包含files, simulation_requirement, project_name等
 * @returns {Promise}
 */
export function generateOntology(formData) {
  return requestWithRetry(() => 
    service({
      url: '/api/graph/ontology/generate',
      method: 'post',
      data: formData,
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  )
}

/**
 * 构建图谱
 * @param {Object} data - 包含project_id, graph_name等
 * @returns {Promise}
 */
export function buildGraph(data) {
  return requestWithRetry(() =>
    service({
      url: '/api/graph/build',
      method: 'post',
      data
    })
  )
}

/**
 * 查询任务状态
 * @param {String} taskId - 任务ID
 * @returns {Promise}
 */
export function getTaskStatus(taskId) {
  return service({
    url: `/api/graph/task/${taskId}`,
    method: 'get'
  })
}

/**
 * 获取图谱数据
 * @param {String} graphId - 图谱ID
 * @returns {Promise}
 */
export function getGraphData(graphId) {
  return service({
    url: `/api/graph/data/${graphId}`,
    method: 'get'
  })
}

/**
 * 获取项目信息
 * @param {String} projectId - 项目ID
 * @returns {Promise}
 */
export function getProject(projectId) {
  return service({
    url: `/api/graph/project/${projectId}`,
    method: 'get'
  })
}

/**
 * List all projects (newest first). Used by the Home view's project
 * gallery so users can pick a project before drilling into its
 * simulations.
 */
export function listProjects(limit = 50) {
  return service({
    url: `/api/graph/project/list`,
    method: 'get',
    params: { limit }
  })
}

/**
 * Update project metadata (currently scoped to simulation_requirement).
 * @param {String} projectId
 * @param {Object} patch  e.g. { simulation_requirement: "..." }
 */
export function updateProject(projectId, patch) {
  return service({
    url: `/api/graph/project/${projectId}`,
    method: 'patch',
    data: patch
  })
}

/**
 * List all simulations under a project (newest first).
 */
export function listProjectSimulations(projectId) {
  return service({
    url: `/api/graph/project/${projectId}/simulations`,
    method: 'get'
  })
}

/**
 * Create a new simulation under a project. simulation_requirement is
 * optional — when supplied, overrides the project's default for this
 * sim only.
 * @param {Object} data { project_id, graph_id?, enable_twitter?,
 *                        enable_reddit?, simulation_requirement? }
 */
export function createSimulation(data) {
  return service({
    url: `/api/simulation/create`,
    method: 'post',
    data
  })
}

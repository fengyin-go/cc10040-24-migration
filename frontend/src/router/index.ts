import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ServiceReferenceView from '@/views/ServiceReferenceView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/service-reference', name: 'service-reference', component: ServiceReferenceView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router

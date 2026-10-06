<template>
  <div>
    <h1>补位意图</h1>
    <p class="muted">FEFO 短量失败自动登记 · 未确认不冻结库存 · 确认才插入新批</p>
    <p v-if="!rows.length" class="muted">暂无补位意图</p>
    <div v-for="i in rows" :key="i.id" class="intent" :class="{ focus: i.id === focus }">
      <div>
        <b>#{{ i.id }} {{ i.name }}</b> 缺口 ×{{ i.qty_short }} {{ i.unit }}
        <span class="muted"> · {{ (i.created_at || '').slice(0, 10) }}</span>
      </div>
      <div v-if="i.status === 'pending'" class="intent-confirm">
        <input v-model="expiry[i.id]" placeholder="到期 YYYY-MM-DD" />
        <button @click="confirm(i)">确认补入</button>
      </div>
      <div v-else class="done">已补 → 新批 #{{ i.lot_id }} · <router-link to="/">看全层</router-link></div>
    </div>
    <p v-if="msg" class="muted">{{ msg }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
const rows = ref([])
const expiry = ref({})
const msg = ref('')
const focus = Number(useRoute().query.focus || 0)
function defaultExpiry() { const d = new Date(); d.setDate(d.getDate() + 30); return d.toISOString().slice(0, 10) }
async function load() {
  rows.value = await api('/intents')
  for (const i of rows.value) if (!expiry.value[i.id]) expiry.value[i.id] = defaultExpiry()
}
async function confirm(i) {
  try {
    const r = await api('/intents/' + i.id + '/confirm', { method: 'POST', body: JSON.stringify({ expiry: expiry.value[i.id] }) })
    msg.value = `意图 #${i.id} 已补入新批 #${r.lot_id}（×${r.qty}），全层可见`
  } catch (e) {
    let d = null
    try { d = JSON.parse(e.message) } catch {}
    if (d && d.reason === 'already_fulfilled') msg.value = `意图 #${i.id} 已补过（新批 #${d.lot_id}），未重复长批`
    else msg.value = '确认失败：' + e.message
  }
  await load()
}
onMounted(load)
</script>

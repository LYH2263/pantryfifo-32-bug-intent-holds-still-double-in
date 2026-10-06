<template>
  <div>
    <h1>按临期消费 · 未确认意图不占库存</h1>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button @click="go">FEFO 扣减</button>
    <pre>{{ result }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const result = ref('')
const router = useRouter()
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })
async function go() {
  try {
    result.value = JSON.stringify(await api('/consume', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }) }), null, 2)
  } catch (e) {
    // 短量失败:后端已登记补位意图,直接进入意图页确认补入
    let d = null
    try { d = JSON.parse(e.message) } catch {}
    if (d && d.reason === 'short' && d.intent_id) {
      router.push({ path: '/intents', query: { focus: d.intent_id } })
      return
    }
    result.value = e.message
  }
}
</script>

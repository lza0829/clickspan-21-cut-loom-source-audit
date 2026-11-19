<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const sec = ref(1800)
const msg = ref('')
const err = ref('')

onMounted(async () => {
  try {
    const s = await api.settings()
    sec.value = s.idle_timeout_sec
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function save() {
  err.value = ''
  msg.value = ''
  try {
    const res = await fetch('/api/settings/idle-timeout', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idle_timeout_sec: Number(sec.value) }),
    })
    if (!res.ok) throw new Error(await res.text())
    const data = await res.json()
    sec.value = data.idle_timeout_sec
    msg.value = '已保存：后续收流按新超时裁决，已关闭会话不回溯重写'
  } catch (e) {
    err.value = String(e.message || e)
  }
}
</script>

<template>
  <section class="panel">
    <h1>空闲切窗</h1>
    <p class="muted">
      秒数写入 settings，收流逐击点按「新击点 − open 末击点」间隙裁决：严格大于该秒数即同事务关旧开新并写切分边表。
    </p>
    <label>
      秒数
      <input v-model.number="sec" type="number" min="60" step="60" class="mono" />
    </label>
    <p>
      <button class="btn" @click="save">保存</button>
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="err">{{ err }}</p>
  </section>
</template>

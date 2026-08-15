<template>
  <router-view />
  <!-- Global pinned disclaimer — rendered on every route -->
  <DisclaimerFooter />
</template>

<script setup>
// Root shell — layout is decided per-route (auth pages vs. main layout);
// only the fixed bottom disclaimer lives here.
import DisclaimerFooter from './components/DisclaimerFooter.vue'
</script>

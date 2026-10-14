//! MEM-NFR-01-01 hot-query cache for `long_term.search_headers`.
//!
//! Agents re-issue the same recall queries (preferences, project facts,
//! settings) many times between writes, so the finished result of the
//! lexical cascade + semantic fusion is memoized per
//! (identity, query, top_k, kinds) and invalidated wholesale through a
//! generation counter. Every mutation that can change recall results —
//! long-term content writes, embedding rows, lexical backfill progress,
//! restored backups, embedding settings — bumps the generation; entries
//! from older generations are unreachable and dropped. Hits skip the scan
//! gate and the connection pool entirely; misses run the unchanged
//! pipeline and repopulate the cache.
//!
//! Disable for diagnostics or production debugging with
//! `AGENT_MEMORY_SEARCH_CACHE=0`.

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::MemoryIdentity;

/// Bounded entry count. Each entry holds at most `top_k` (<=100) header
/// objects, so the worst-case footprint stays in the low hundreds of KB.
const MAX_ENTRIES: usize = 128;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct CacheKey {
    account_id: String,
    agent_id: String,
    memory_space_id: String,
    query: String,
    top_k: i64,
    kinds: Vec<String>,
    generation: u64,
}

impl CacheKey {
    fn new(
        identity: &MemoryIdentity,
        query: &str,
        top_k: i64,
        kinds: &[String],
        generation: u64,
    ) -> Self {
        Self {
            account_id: identity.account_id.clone(),
            agent_id: identity.agent_id.clone(),
            memory_space_id: identity.memory_space_id.clone(),
            query: query.to_owned(),
            top_k,
            kinds: kinds.to_vec(),
            generation,
        }
    }
}

#[derive(Debug, Default)]
struct CacheState {
    map: HashMap<CacheKey, Arc<Vec<Value>>>,
    order: VecDeque<CacheKey>,
}

/// A lookup opened at the start of a search. The invalidation generation is
/// captured here, before the pipeline runs: a write landing mid-pipeline
/// then makes the finished result land under a dead generation — invisible
/// to future lookups — instead of caching stale data.
pub(crate) struct CacheLookup {
    enabled: bool,
    key: Option<CacheKey>,
}

#[derive(Debug)]
pub(crate) struct SearchResultCache {
    generation: AtomicU64,
    enabled: bool,
    state: Mutex<CacheState>,
}

impl SearchResultCache {
    pub(crate) fn new() -> Self {
        let enabled = std::env::var("AGENT_MEMORY_SEARCH_CACHE")
            .map(|value| value.trim() != "0")
            .unwrap_or(true);
        Self {
            generation: AtomicU64::new(1),
            enabled,
            state: Mutex::new(CacheState::default()),
        }
    }

    /// Drop every memoized result. Called after any mutation that can change
    /// recall output; wholesale invalidation keeps the read path simple, and
    /// writes are rare relative to recall so a few recomputed queries cost
    /// nothing.
    pub(crate) fn invalidate(&self) {
        if !self.enabled {
            return;
        }
        self.generation.fetch_add(1, Ordering::Release);
        if let Ok(mut state) = self.state.lock() {
            state.map.clear();
            state.order.clear();
        }
    }

    pub(crate) fn begin(
        &self,
        identity: &MemoryIdentity,
        query: &str,
        top_k: i64,
        kinds: &[String],
    ) -> CacheLookup {
        if !self.enabled {
            return CacheLookup { enabled: false, key: None };
        }
        let generation = self.generation.load(Ordering::Acquire);
        CacheLookup {
            enabled: true,
            key: Some(CacheKey::new(identity, query, top_k, kinds, generation)),
        }
    }

    pub(crate) fn get(&self, lookup: &CacheLookup) -> Option<Arc<Vec<Value>>> {
        let key = lookup.key.as_ref()?;
        self.state.lock().ok()?.map.get(key).cloned()
    }

    pub(crate) fn insert(&self, lookup: &CacheLookup, values: Vec<Value>) {
        let Some(key) = lookup.key.as_ref() else {
            return;
        };
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        if state.map.contains_key(key) {
            return;
        }
        while state.order.len() >= MAX_ENTRIES {
            match state.order.pop_front() {
                Some(evicted) => {
                    state.map.remove(&evicted);
                }
                None => break,
            }
        }
        state.order.push_back(key.clone());
        state.map.insert(key.clone(), Arc::new(values));
    }
}

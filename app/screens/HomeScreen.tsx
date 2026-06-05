// app/screens/HomeScreen.tsx
import React, { useState, useEffect, useCallback } from "react";
import {
  View, Text, FlatList, TouchableOpacity,
  StyleSheet, Alert, RefreshControl, SafeAreaView
} from "react-native";
import { useFocusEffect } from "@react-navigation/native";
import { api } from "../api/client";

const CATEGORY_EMOJI: Record<string, string> = {
  식비: "🍚", 교통: "🚌", 쇼핑: "🛍️",
  의료: "💊", 문화: "🎬", 미분류: "📋", 기타: "📌",
};

export default function HomeScreen({ navigation }: any) {
  const now = new Date();
  const [year, setYear]       = useState(now.getFullYear());
  const [month, setMonth]     = useState(now.getMonth() + 1);
  const [expenses, setExpenses] = useState<any[]>([]);
  const [summary, setSummary]   = useState<any>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    const [expList, sum] = await Promise.all([
      api.getExpenses(year, month),
      api.getSummary(year, month),
    ]);
    setExpenses(expList);
    setSummary(sum);
  };

  useFocusEffect(useCallback(() => { load(); }, [year, month]));

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const handleDelete = (id: string) => {
    Alert.alert("삭제", "이 지출을 삭제할까요?", [
      { text: "취소", style: "cancel" },
      {
        text: "삭제", style: "destructive", onPress: async () => {
          await api.deleteExpense(id);
          load();
        }
      },
    ]);
  };

  const prevMonth = () => {
    if (month === 1) { setYear(y => y - 1); setMonth(12); }
    else setMonth(m => m - 1);
  };
  const nextMonth = () => {
    if (month === 12) { setYear(y => y + 1); setMonth(1); }
    else setMonth(m => m + 1);
  };

  return (
    <SafeAreaView style={s.container}>
      {/* 월 네비게이션 */}
      <View style={s.header}>
        <TouchableOpacity onPress={prevMonth}><Text style={s.arrow}>‹</Text></TouchableOpacity>
        <Text style={s.headerTitle}>{year}년 {month}월</Text>
        <TouchableOpacity onPress={nextMonth}><Text style={s.arrow}>›</Text></TouchableOpacity>
      </View>

      {/* 월간 요약 카드 */}
      {summary && (
        <View style={s.summaryCard}>
          <Text style={s.summaryLabel}>이번 달 총 지출</Text>
          <Text style={s.summaryAmount}>
            {summary.total.toLocaleString()}원
          </Text>
          {summary.top_store && (
            <Text style={s.summaryTop}>최다 방문: {summary.top_store}</Text>
          )}
          <View style={s.categoryRow}>
            {Object.entries(summary.by_category || {}).map(([cat, amt]: any) => (
              <View key={cat} style={s.categoryChip}>
                <Text style={s.categoryChipText}>
                  {CATEGORY_EMOJI[cat] || "📌"} {cat} {amt.toLocaleString()}원
                </Text>
              </View>
            ))}
          </View>
        </View>
      )}

      {/* 지출 목록 */}
      <FlatList
        data={expenses}
        keyExtractor={item => item.id}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
        ListEmptyComponent={
          <Text style={s.empty}>지출 내역이 없어요{"\n"}아래 + 버튼으로 추가해보세요</Text>
        }
        renderItem={({ item }) => (
          <TouchableOpacity
            style={s.item}
            onLongPress={() => handleDelete(item.id)}
          >
            <View style={s.itemLeft}>
              <Text style={s.itemEmoji}>{CATEGORY_EMOJI[item.category] || "📌"}</Text>
              <View>
                <Text style={s.itemStore}>{item.store_name}</Text>
                <Text style={s.itemMeta}>{item.date} · {item.category}</Text>
                {item.memo && <Text style={s.itemMemo}>{item.memo}</Text>}
              </View>
            </View>
            <Text style={s.itemAmount}>{item.amount.toLocaleString()}원</Text>
          </TouchableOpacity>
        )}
      />

      {/* 하단 버튼 */}
      <View style={s.fab}>
        <TouchableOpacity
          style={s.fabBtn}
          onPress={() => navigation.navigate("Input")}
        >
          <Text style={s.fabText}>＋</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[s.fabBtn, s.fabChat]}
          onPress={() => navigation.navigate("Chat")}
        >
          <Text style={s.fabText}>💬</Text>
        </TouchableOpacity>
      </View>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  container:        { flex: 1, backgroundColor: "#F8F9FA" },
  header:           { flexDirection: "row", justifyContent: "space-between", alignItems: "center", padding: 20 },
  headerTitle:      { fontSize: 20, fontWeight: "700", color: "#1A1A2E" },
  arrow:            { fontSize: 28, color: "#4A90D9", paddingHorizontal: 10 },
  summaryCard:      { margin: 16, padding: 20, backgroundColor: "#1A1A2E", borderRadius: 16 },
  summaryLabel:     { color: "#8899AA", fontSize: 13 },
  summaryAmount:    { color: "#FFFFFF", fontSize: 32, fontWeight: "800", marginVertical: 4 },
  summaryTop:       { color: "#8899AA", fontSize: 12, marginBottom: 12 },
  categoryRow:      { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  categoryChip:     { backgroundColor: "#2A2A4E", paddingHorizontal: 10, paddingVertical: 4, borderRadius: 20 },
  categoryChipText: { color: "#AABBCC", fontSize: 12 },
  item:             { flexDirection: "row", justifyContent: "space-between", alignItems: "center", backgroundColor: "#FFF", marginHorizontal: 16, marginVertical: 4, padding: 16, borderRadius: 12 },
  itemLeft:         { flexDirection: "row", alignItems: "center", gap: 12 },
  itemEmoji:        { fontSize: 24 },
  itemStore:        { fontSize: 15, fontWeight: "600", color: "#1A1A2E" },
  itemMeta:         { fontSize: 12, color: "#8899AA", marginTop: 2 },
  itemMemo:         { fontSize: 12, color: "#AAAAAA", marginTop: 2 },
  itemAmount:       { fontSize: 16, fontWeight: "700", color: "#1A1A2E" },
  empty:            { textAlign: "center", color: "#AAAAAA", marginTop: 60, lineHeight: 26 },
  fab:              { position: "absolute", bottom: 32, right: 20, gap: 12 },
  fabBtn:           { width: 56, height: 56, borderRadius: 28, backgroundColor: "#4A90D9", justifyContent: "center", alignItems: "center", elevation: 4 },
  fabChat:          { backgroundColor: "#2A2A4E" },
  fabText:          { color: "#FFF", fontSize: 22 },
});

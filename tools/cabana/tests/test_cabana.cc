
#include "opendbc/can/common.h"
#undef INFO
#include "catch2/catch.hpp"
#include "tools/replay/logreader.h"
#include "tools/cabana/dbc/dbcmanager.h"
#include "tools/cabana/streams/abstractstream.h"

// demo route, first segment
const std::string TEST_RLOG_URL = "https://commadata2.blob.core.windows.net/commadata2/4cf7a6ad03080c90/2021-09-29--13-46-36/0/rlog.bz2";

TEST_CASE("DBCFile::generateDBC") {
  QString fn = QString("%1/%2.dbc").arg(OPENDBC_FILE_PATH, "toyota_new_mc_pt_generated");
  DBCFile dbc_origin(fn);
  DBCFile dbc_from_generated("", dbc_origin.generateDBC());

  REQUIRE(dbc_origin.msgCount() == dbc_from_generated.msgCount());
  auto msgs = dbc_origin.getMessages();
  auto new_msgs = dbc_from_generated.getMessages();
  for (auto &[id, m] : msgs) {
    auto &new_m = new_msgs.at(id);
    REQUIRE(m.name == new_m.name);
    REQUIRE(m.size == new_m.size);
    REQUIRE(m.getSignals().size() == new_m.getSignals().size());
    auto sigs = m.getSignals();
    auto new_sigs = new_m.getSignals();
    for (int i = 0; i < sigs.size(); ++i) {
      REQUIRE(*sigs[i] == *new_sigs[i]);
    }
  }
}

TEST_CASE("Parse can messages") {
  DBCManager dbc(nullptr);
  dbc.open({0}, "toyota_new_mc_pt_generated");
  CANParser can_parser(0, "toyota_new_mc_pt_generated", {}, {});

  LogReader log;
  REQUIRE(log.load(TEST_RLOG_URL, nullptr, {}, true));
  REQUIRE(log.events.size() > 0);
  for (auto e : log.events) {
    if (e->which == cereal::Event::Which::CAN) {
      std::map<std::pair<uint32_t, QString>, std::vector<double>> values_1;
      for (const auto &c : e->event.getCan()) {
        const auto msg = dbc.msg({.source = c.getSrc(), .address = c.getAddress()});
        if (c.getSrc() == 0 && msg) {
          for (auto sig : msg->getSignals()) {
            double val = get_raw_value((uint8_t *)c.getDat().begin(), c.getDat().size(), *sig);
            values_1[{c.getAddress(), sig->name}].push_back(val);
          }
        }
      }

      can_parser.UpdateCans(e->mono_time, e->event.getCan());
      std::vector<SignalValue> values_2;
      can_parser.query_latest(values_2);
      for (auto &[key, v1] : values_1) {
        bool found = false;
        for (auto &v2 : values_2) {
          if (v2.address == key.first && key.second == v2.name.c_str()) {
            REQUIRE(v2.all_values.size() == v1.size());
            REQUIRE(v2.all_values == v1);
            found = true;
            break;
          }
        }
        REQUIRE(found);
      }
    }
  }
}

TEST_CASE("Parse dbc") {
  QString content = R"(
BO_ 160 message_1: 8 XXX
  SG_ signal_1 : 0|12@1+ (1,0) [0|4095] "unit"  XXX
  SG_ signal_2 : 12|1@1+ (1.0,0.0) [0.0|1] ""  XXX

VAL_ 160 signal_1 0 "disabled" 1.2 "initializing" 2 "fault";

CM_ BO_ 160 "message comment" ;
CM_ SG_ 160 signal_1 "signal comment";
CM_ SG_ 160 signal_2 "multiple line comment
1
2
";)";

  DBCFile file("", content);
  auto msg = file.msg(160);
  REQUIRE(msg != nullptr);
  REQUIRE(msg->name == "message_1");
  REQUIRE(msg->size == 8);
  REQUIRE(msg->comment == "message comment");
  REQUIRE(msg->sigs.size() == 2);
  REQUIRE(file.msg("message_1") != nullptr);

  auto sig_1 = msg->sigs[0];
  REQUIRE(sig_1->name == "signal_1");
  REQUIRE(sig_1->start_bit == 0);
  REQUIRE(sig_1->size == 12);
  REQUIRE(sig_1->min == 0);
  REQUIRE(sig_1->max == 4095);
  REQUIRE(sig_1->unit == "unit");
  REQUIRE(sig_1->comment == "signal comment");
  REQUIRE(sig_1->val_desc.size() == 3);
  REQUIRE(sig_1->val_desc[0] == std::pair<double, QString>{0, "disabled"});
  REQUIRE(sig_1->val_desc[1] == std::pair<double, QString>{1.2, "initializing"});
  REQUIRE(sig_1->val_desc[2] == std::pair<double, QString>{2, "fault"});

  auto &sig_2 = msg->sigs[1];
  REQUIRE(sig_2->comment == "multiple line comment\n1\n2");
}

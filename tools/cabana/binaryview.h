#pragma once

#include <QApplication>
#include <QList>
#include <QSet>
#include <QStyledItemDelegate>
#include <QTableView>

#include "tools/cabana/dbc/dbcmanager.h"
#include "tools/cabana/streams/abstractstream.h"

class BinaryItemDelegate : public QStyledItemDelegate {
public:
  BinaryItemDelegate(QObject *parent);
  void paint(QPainter *painter, const QStyleOptionViewItem &option, const QModelIndex &index) const override;
  void setSelectionColor(const QColor &color) { selection_color = color; }
  bool hasSignal(const QModelIndex &index, int dx, int dy, const cabana::Signal *sig) const;
  void drawSignalCell(QPainter* painter, const QStyleOptionViewItem &option, const QModelIndex &index, const cabana::Signal *sig) const;

  QFont small_font, hex_font;
  QColor selection_color;
};

class BinaryViewModel : public QAbstractTableModel {
public:
  BinaryViewModel(QObject *parent) : QAbstractTableModel(parent) {}
  void refresh();
  void updateState();
  void updateItem(int row, int col, const QString &val, const QColor &color);
  QVariant headerData(int section, Qt::Orientation orientation, int role = Qt::DisplayRole) const override;
  QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const { return {}; }
  int rowCount(const QModelIndex &parent = QModelIndex()) const override { return row_count; }
  int columnCount(const QModelIndex &parent = QModelIndex()) const override { return column_count; }
  inline QModelIndex bitIndex(int bit, bool is_lb) const { return index(bit / 8, is_lb ? (7 - bit % 8) : bit % 8); }
  QModelIndex index(int row, int column, const QModelIndex &parent = QModelIndex()) const override {
    return createIndex(row, column, (void *)&items[row * column_count + column]);
  }
  Qt::ItemFlags flags(const QModelIndex &index) const override {
    return (index.column() == column_count - 1) ? Qt::ItemIsEnabled : Qt::ItemIsEnabled | Qt::ItemIsSelectable;
  }

  struct Item {
    QColor bg_color = QColor(102, 86, 169, 255);
    bool is_msb = false;
    bool is_lsb = false;
    QString val;
    QList<const cabana::Signal *> sigs;
    bool valid = false;
  };
  std::vector<Item> items;

  MessageId msg_id;
  int row_count = 0;
  const int column_count = 9;
};

class BinaryView : public QTableView {
  Q_OBJECT

public:
  BinaryView(QWidget *parent = nullptr);
  void setMessage(const MessageId &message_id);
  void highlight(const cabana::Signal *sig);
  QSet<const cabana::Signal*> getOverlappingSignals() const;
  inline void updateState() { model->updateState(); }
  QSize minimumSizeHint() const override;

signals:
  void signalClicked(const cabana::Signal *sig);
  void signalHovered(const cabana::Signal *sig);
  void addSignal(int start_bit, int size, bool little_endian);
  void resizeSignal(const cabana::Signal *sig, int from, int size);
  void removeSignal(const cabana::Signal *sig);
  void editSignal(const cabana::Signal *origin_s, cabana::Signal &s);
  void showChart(const MessageId &id, const cabana::Signal *sig, bool show, bool merge);

private:
  void addShortcuts();
  void refresh();
  std::tuple<int, int, bool> getSelection(QModelIndex index);
  void setSelection(const QRect &rect, QItemSelectionModel::SelectionFlags flags) override;
  void mousePressEvent(QMouseEvent *event) override;
  void mouseMoveEvent(QMouseEvent *event) override;
  void mouseReleaseEvent(QMouseEvent *event) override;
  void leaveEvent(QEvent *event) override;
  void highlightPosition(const QPoint &pt);

  QModelIndex anchor_index;
  BinaryViewModel *model;
  BinaryItemDelegate *delegate;
  const cabana::Signal *resize_sig = nullptr;
  const cabana::Signal *hovered_sig = nullptr;
  friend class BinaryItemDelegate;
};

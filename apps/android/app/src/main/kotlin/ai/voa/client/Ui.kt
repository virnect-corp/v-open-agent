package ai.voa.client

import android.app.Activity
import android.graphics.Typeface
import android.view.Gravity
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

// 작은 화면 도우미 (빌드 도구나 UI 라이브러리를 늘리지 않으려고 View를 코드로 만든다)
fun Activity.title(text: String) = TextView(this).apply {
    this.text = text
    textSize = 24f
    setTypeface(typeface, Typeface.BOLD)
}

fun Activity.muted(text: String) = TextView(this).apply {
    this.text = text
    setTextColor(0xFF5D6673.toInt())
}

fun Activity.field(label: String, value: String, inputType: Int): Pair<View, EditText> {
    val edit = EditText(this).apply {
        setText(value)
        this.inputType = inputType
        setSingleLine()
    }
    val box = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(TextView(context).apply { text = label })
        addView(edit)
    }
    return box to edit
}

fun Activity.column(vararg views: View): View {
    val inner = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.TOP
        val pad = (24 * resources.displayMetrics.density).toInt()
        setPadding(pad, pad, pad, pad)
        views.forEach { addView(it) }
    }
    return ScrollView(this).apply { addView(inner) }
}
